"""Tests of the EIOPA & QRT agent (no network: the chat model is a scripted fake that really calls the tools).
Run: python -m pytest eiopa_agent/tests -q   (from rag_service/)"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from openpyxl import load_workbook

from eiopa_agent import agent as agent_module
from eiopa_agent import excel_qrt
from eiopa_agent.agent import SYSTEM_PROMPT, AgentRequest, run_agent
from eiopa_agent.own_funds import OwnFundsItem, OwnFundsPortfolio, compute_eligibility
from eiopa_agent.tools import KB_CONFIRM_SCORE, AgentContext, build_tools
from pipeline.agents.data_quality_agent import audit_portfolio
from pipeline.schemas import PortfolioPayload


def funds(t1u, t1r, t2, t3, scr, mcr) -> OwnFundsPortfolio:
    return OwnFundsPortfolio(scr=scr, mcr=mcr, reference_date="2026-12-31", items=[
        OwnFundsItem(name="Capital social et réserves", tier="T1_UNRESTRICTED", amount=t1u, kind="capital"),
        OwnFundsItem(name="Titres hybrides", tier="T1_RESTRICTED", amount=t1r, kind="dette hybride"),
        OwnFundsItem(name="Obligations subordonnées 2034", tier="T2", amount=t2, kind="dette subordonnée"),
        OwnFundsItem(name="Actifs d'impôts différés nets", tier="T3", amount=t3, kind="DTA")])


MUTUELLE = funds(228.5, 18, 95, 42, 251.17, 113.03)          # the figures of the n8n engine on 2_Mutuelle_IARD_Dominante
SOUS_TENSION = funds(118, 38, 135, 52, 316.07, 78)


def hit(text="Les fonds propres de niveau 2 sont limités à 50 % du SCR.", source="1.Pilier1.pdf", page=12, score=0.82):
    return {"id": "x", "text": text, "meta": {"source": source, "page_start": page, "page_end": page}, "score": score}


# ----------------------------------------------------------------------------- own funds (deterministic)


def test_eligibility_reproduces_the_n8n_engine():
    e = compute_eligibility(MUTUELLE)
    assert (e.available, e.eligible_scr, e.eligible_mcr) == (383.5, 372.09, 269.11)
    # the engine works from the unrounded SCR (251.17x); the agent gets the published 251.17: the haircut may differ by one cent
    assert e.haircut == pytest.approx(11.41, abs=0.011)
    assert (e.ratio_scr_pct, e.ratio_mcr_pct) == (148.14, 238.08)
    assert e.t3_eligible == 37.68 and e.t2_eligible == 87.91 and e.reclassified_to_t2 == 0
    assert e.t2_plus_t3_classified == 137                       # given to the model so that it never has to add anything itself
    assert e.qualitative_criteria_checked is False


def test_breach_case_reclassifies_and_flags_the_tier1_floor():
    e = compute_eligibility(SOUS_TENSION)
    assert e.reclassified_to_t2 == 8.5 and e.t1_eligible == 147.5
    assert e.eligible_scr == pytest.approx(305.53, abs=0.011) and e.ratio_scr_pct == pytest.approx(96.67, abs=0.01)
    checks = {c.name: c for c in e.checks}
    assert not checks["Tier 1 ≥ 50 % du SCR"].ok and not checks["Tier 3 < 15 % du SCR"].ok and not checks["Ratio de couverture SCR ≥ 100 %"].ok
    assert "8.50" in checks["Tier 1 restreint ≤ 20 % du Tier 1"].note


def test_caps_are_not_reported_as_breaches():
    """An eligibility ceiling being exceeded is PLAFONNÉ (excess not eligible); only a missed minimum is an ÉCART."""
    checks = {c.name: c for c in compute_eligibility(MUTUELLE).checks}
    assert checks["Tier 2 ≤ 20 % du MCR"].status == "PLAFONNÉ" and "72.39" in checks["Tier 2 ≤ 20 % du MCR"].note
    assert checks["Tier 2 + Tier 3 ≤ 50 % du SCR"].status == "PLAFONNÉ" and checks["Tier 3 < 15 % du SCR"].status == "PLAFONNÉ"
    assert checks["Tier 1 ≥ 50 % du SCR"].status == "OK"
    breach = {c.name: c for c in compute_eligibility(SOUS_TENSION).checks}
    assert breach["Tier 1 ≥ 50 % du SCR"].status == "ÉCART" and breach["Ratio de couverture SCR ≥ 100 %"].status == "ÉCART"


def test_zero_mcr_skips_the_mcr_checks():
    e = compute_eligibility(funds(100, 0, 20, 0, 80, 0))
    assert e.ratio_mcr_pct is None and not any("MCR" in c.name for c in e.checks)


# ----------------------------------------------------------------------------- excel generation


@pytest.fixture(autouse=True)
def tmp_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_qrt, "GENERATED_DIR", tmp_path / "generated")


def test_s2301_workbook_layout_and_values():
    e = compute_eligibility(MUTUELLE)
    f = excel_qrt.write_qrt("S.23.01", MUTUELLE, e, [{"id": 1, "source": "cours.pdf", "page": 3, "score": 0.9, "excerpt": "limites"}], True, [1], "Tier 2 ?")
    wb = load_workbook(f.path)
    assert wb.sheetnames == ["Fonds_Propres_Ratios", "Synthese_Instruments", "Controles_Limites", "Synthese_Sources"]
    rows = {r[1]: r[2] for r in wb["Fonds_Propres_Ratios"].iter_rows(min_row=2, values_only=True) if r[1]}
    assert rows == {
        "Tier 1 non restreint": 228.5,
        "Tier 1 restreint": 18,
        "Tier 2 (dettes subordonnées)": 95,
        "Tier 3 (DTA nets)": 42,
        "Fonds propres disponibles totaux": 383.5,
    }
    ctrl = [r for r in wb["Controles_Limites"].iter_rows(min_row=2, values_only=True) if r[0]]
    assert all(r[4] in ("OK", "PLAFONNÉ", "ÉCART") for r in ctrl if r[4]) and any(r[4] == "PLAFONNÉ" for r in ctrl)
    sources = wb["Synthese_Sources"]
    assert sources["B1"].value == "OUI" and sources["B2"].value == "[1]" and sources["B3"].value == 0.9
    assert sources["B8"].value == "cours.pdf" and sources["E8"].value == "oui"


def test_sheets_other_than_the_input_are_ignored_by_the_app_and_the_engine():
    """The app and the n8n engine skip sheets whose name matches /pieuvre|synthese|matric|controle|result/."""
    import re
    ignored = re.compile(r"pieuvre|synthese|matric|controle|result", re.I)
    f = excel_qrt.write_qrt("S.23.01", MUTUELLE, compute_eligibility(MUTUELLE), [], None)
    assert [s for s in load_workbook(f.path).sheetnames if not ignored.search(s)] == ["Fonds_Propres_Ratios"]
    assert load_workbook(f.path)["Synthese_Sources"]["B1"].value == "non évalué"


def test_generated_file_goes_back_through_our_own_data_quality_agent():
    f = excel_qrt.write_qrt("S.23.01", MUTUELLE, compute_eligibility(MUTUELLE), [], None)
    ws = load_workbook(f.path)["Fonds_Propres_Ratios"]
    heads = [c.value for c in ws[1]]
    positions = [{"_sheet": "Fonds_Propres_Ratios", **dict(zip(heads, r))} for r in ws.iter_rows(min_row=2, values_only=True)]
    report = audit_portfolio(PortfolioPayload(source={"system": "x", "extraction_date": "2026-10-27", "reference": "r"}, positions=positions))
    assert report.status != "BLOCKED" and report.rows_audited == len(positions) == 5


def test_s2501_needs_the_scr_result():
    e = compute_eligibility(MUTUELLE)
    with pytest.raises(ValueError, match="modules de risque"):
        excel_qrt.write_qrt("S.25.01", MUTUELLE, e, [], None)
    solvency = {"modules": [{"name": "Marché", "value": 108.73}, {"name": "Souscription Non-Vie", "value": 163.11}, {"name": "Vie", "value": 14.3}],
                "details": {"bscr": 250.48, "diversificationBenefit": -112.42}}
    f = excel_qrt.write_qrt("S.25.01", MUTUELLE, e, [], None, solvency=solvency)
    rows = {r[0]: r[3] for r in load_workbook(f.path)["S2501_SCR"].iter_rows(min_row=2, values_only=True) if r[0]}          # column D = Net (C0030)
    assert rows["S.25.01-R0010"] == 108.73 and rows["S.25.01-R0050"] == 163.11
    assert rows["S.25.01-R0030"] == 14.3 and rows["S.25.01-R0100"] == 250.48          # "Non-Vie" must not land on the Vie line (it contains "vie")
    assert "S.25.01-R0200" not in rows


def test_unknown_template_is_refused():
    with pytest.raises(ValueError, match="inconnu"):
        excel_qrt.write_qrt("S.99.99", MUTUELLE, compute_eligibility(MUTUELLE), [], None)


# ----------------------------------------------------------------------------- tools


def tool_by_name(ctx, name):
    return next(t for t in build_tools(ctx) if t.name == name)


def make_ctx(retriever=lambda q: [hit()], portfolio=MUTUELLE, solvency=None):
    return AgentContext(question="Tier 2 ?", portfolio=portfolio, solvency=solvency, retriever=retriever)


def test_search_returns_numbered_passages_and_records_them():
    ctx = make_ctx(lambda q: [hit(), hit("Tier 3 inférieur à 15 % du SCR", page=13, score=0.2)])
    out = tool_by_name(ctx, "search_eiopa_vector_db").invoke({"query": "limites Tier 2"})
    assert "[1] 1.Pilier1.pdf (p. 12) — pertinence 0.82" in out and "[2]" in out
    assert [s["id"] for s in ctx.sources] == [1, 2] and ctx.kb_max_relevance == 0.82
    tool_by_name(ctx, "search_eiopa_vector_db").invoke({"query": "limites Tier 2"})              # same passages again: no duplicates
    assert len(ctx.sources) == 2 and ctx.searches == 2


def test_a_retrieval_score_alone_never_counts_as_a_confirmation():
    """Live finding: passages about the SCR/MCR in general scored 0.41-0.45 without stating the Tier limits."""
    ctx = make_ctx(lambda q: [hit("Le SCR est calibré à 99,5 %.", score=0.45)])
    tool_by_name(ctx, "search_eiopa_vector_db").invoke({"query": "limites Tier 2"})
    tool_by_name(ctx, "check_tier_limits").invoke({})                                            # the agent declares nothing
    assert ctx.kb_max_relevance == 0.45 and ctx.rule_confirmed is False and ctx.evidence_ids == []


def test_a_declared_confirmation_is_checked_against_its_evidence():
    ctx = make_ctx(lambda q: [hit(score=0.82), hit("autre", page=9, score=KB_CONFIRM_SCORE - 0.1)])
    tool_by_name(ctx, "search_eiopa_vector_db").invoke({"query": "x"})
    check = tool_by_name(ctx, "check_tier_limits")
    check.invoke({"rule_confirmed": True, "evidence_ids": [1]})
    assert ctx.rule_confirmed is True and ctx.evidence_ids == [1]
    out = check.invoke({"rule_confirmed": True, "evidence_ids": [2]})                            # cited passage is too weak
    assert ctx.rule_confirmed is False and ctx.evidence_ids == [] and "Confirmation refusée" in out
    check.invoke({"rule_confirmed": True, "evidence_ids": [42]})                                 # cited passage does not exist
    assert ctx.rule_confirmed is False
    check.invoke({"rule_confirmed": True, "evidence_ids": []})                                   # a claim without proof
    assert ctx.rule_confirmed is False


def test_missing_passages_say_so():
    none = make_ctx(lambda q: [])
    assert "NON confirmée" in tool_by_name(none, "search_eiopa_vector_db").invoke({"query": "x"}) and none.kb_max_relevance == 0


def test_portfolio_tool_exposes_only_the_context_data():
    out = tool_by_name(make_ctx(), "get_own_funds_portfolio").invoke({})
    assert "Obligations subordonnées 2034" in out and "251.17" in out and "Tier 2" in out


def test_check_tier_limits_stores_the_computation():
    ctx = make_ctx()
    out = tool_by_name(ctx, "check_tier_limits").invoke({})
    assert ctx.eligibility.eligible_scr == 372.09 and '"eligible_scr": 372.09' in out and "qualitatifs" in out
    assert '"statut": "PLAFONNÉ"' in out and '"t2_plus_t3_classified": 137.0' in out


def test_generate_excel_reports_failures_as_text_not_exceptions():
    ctx = make_ctx()
    out = tool_by_name(ctx, "generate_excel_qrt").invoke({"template": "S.25.01"})
    assert out.startswith("Impossible de générer") and ctx.file is None
    ok = tool_by_name(ctx, "generate_excel_qrt").invoke({"template": "S.23.01"})
    assert ctx.file and ctx.file.name in ok and "non" in ok.split("base documentaire :")[1]      # nothing searched yet: not confirmed


# ----------------------------------------------------------------------------- the agent (scripted fake model)


class ScriptedChat(GenericFakeChatModel):
    """Plays a fixed list of messages; `bind_tools` is a no-op so create_agent accepts it."""

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args=None, id_="1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id_}])


def full_script():
    return iter([call("search_eiopa_vector_db", {"query": "limites Tier 2"}, "a"), call("get_own_funds_portfolio", {}, "b"),
                 call("check_tier_limits", {"rule_confirmed": True, "evidence_ids": [1]}, "c"), call("generate_excel_qrt", {"template": "S.23.01"}, "d"),
                 AIMessage(content="Le Tier 2 respecte les limites [1]. Fonds propres éligibles : 372,09 M€. Fichier généré.")])


def test_the_agent_calls_the_tools_in_order_and_returns_verifiable_state():
    response = run_agent(AgentRequest(question="Vérifie le Tier 2 et génère le S.23.01", own_funds=MUTUELLE),
                         retriever=lambda q: [hit()], llm_factory=lambda model: ScriptedChat(messages=full_script()))
    assert response.status == "COMPLETED" and response.model == agent_module.MODELS[0]
    assert [t.tool for t in response.tool_calls] == ["search_eiopa_vector_db", "get_own_funds_portfolio", "check_tier_limits", "generate_excel_qrt"]
    assert response.rule_confirmed is True and response.evidence_ids == [1] and response.kb_max_relevance == 0.82
    assert response.sources[0]["source"] == "1.Pilier1.pdf"
    assert response.eligibility.eligible_scr == 372.09                                           # computed by the tool, not by the model
    assert response.file.download_url == f"/api/eiopa-agent/files/{response.file.name}"
    assert load_workbook(excel_qrt.GENERATED_DIR / response.file.name).sheetnames[0] == "Fonds_Propres_Ratios"
    assert "path" not in response.file.model_dump() and str(excel_qrt.GENERATED_DIR) not in response.model_dump_json()      # no server path leaks to the client
    assert "372,09" in response.answer


def test_a_question_without_file_request_generates_no_file():
    script = iter([call("search_eiopa_vector_db", {"query": "q"}, "a"), call("check_tier_limits", {}, "b"), AIMessage(content="Conforme.")])
    response = run_agent(AgentRequest(question="Le Tier 2 est-il conforme ?", own_funds=MUTUELLE), retriever=lambda q: [], llm_factory=lambda m: ScriptedChat(messages=script))
    assert response.file is None and response.rule_confirmed is False and response.eligibility is not None


def test_fallback_model_takes_over():
    calls = []

    def factory(model):
        calls.append(model)
        if model == agent_module.MODELS[0]:
            raise RuntimeError("503 UNAVAILABLE high demand")
        return ScriptedChat(messages=full_script())

    response = run_agent(AgentRequest(question="Génère le S.23.01", own_funds=MUTUELLE), retriever=lambda q: [hit()], llm_factory=factory)
    assert response.status == "COMPLETED" and response.model == agent_module.MODELS[1] and any("503" in e for e in response.errors)


def test_llm_down_still_returns_the_deterministic_eligibility():
    def down(model):
        raise RuntimeError("quota")

    response = run_agent(AgentRequest(question="Génère le S.23.01", own_funds=SOUS_TENSION), llm_factory=down)
    assert response.status == "LLM_UNAVAILABLE" and response.answer is None and response.file is None
    assert response.eligibility.ratio_scr_pct == pytest.approx(96.67, abs=0.01) and len(response.errors) == len(agent_module.MODELS)


def test_system_prompt_imposes_the_search_first_and_the_honesty_rules():
    assert "TOUJOURS par au moins une recherche" in SYSTEM_PROMPT and "règle non confirmée par la base documentaire" in SYSTEM_PROMPT
    assert "dernières guidelines" in SYSTEM_PROMPT and "critères qualitatifs" in SYSTEM_PROMPT


# ----------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(monkeypatch):
    from main import app
    monkeypatch.setattr(agent_module, "_make_llm", lambda model, key=None: ScriptedChat(messages=full_script()))
    monkeypatch.setattr(agent_module, "default_retriever", lambda q: [hit()])
    return TestClient(app)


BODY = {"question": "Vérifie le Tier 2 et génère le S.23.01", "own_funds": MUTUELLE.model_dump()}


def test_run_endpoint_and_download(client):
    response = client.post("/api/eiopa-agent/run", json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED" and body["file"]["template"] == "S.23.01" and body["eligibility"]["eligible_scr"] == 372.09
    download = client.get(body["file"]["download_url"])
    assert download.status_code == 200 and download.content[:2] == b"PK" and "spreadsheetml" in download.headers["content-type"]


def test_download_refuses_anything_that_is_not_a_generated_workbook(client):
    for name in ("..%2F.env", "..%5C..%5C.env", "notes.txt", "QRT_S2301_missing.xlsx"):
        assert client.get(f"/api/eiopa-agent/files/{name}").status_code == 404


def test_run_endpoint_validates_the_request(client):
    assert client.post("/api/eiopa-agent/run", json={"question": "ok", "own_funds": MUTUELLE.model_dump()}).status_code == 422          # question too short
    assert client.post("/api/eiopa-agent/run", json={"question": "Vérifie le Tier 2", "own_funds": {**MUTUELLE.model_dump(), "scr": 0}}).status_code == 422
    assert client.post("/api/eiopa-agent/run", json={"question": "Vérifie le Tier 2", "own_funds": {**MUTUELLE.model_dump(), "items": []}}).status_code == 422
