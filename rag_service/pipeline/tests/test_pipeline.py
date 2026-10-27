"""Graph, guardrail and endpoint tests with a stubbed LLM (no network, no API key). Run: pytest pipeline/tests -q"""
import pytest
from fastapi.testclient import TestClient

from pipeline.agents import risk_analyst_agent as analyst
from pipeline.schemas import Finding, RiskDiagnosisReport, SolvencyMetrics

SOURCE = {"system": "Core-SI", "extraction_date": "2026-12-31", "reference": "LOT-0042"}
SOLVENCY = {"ratio": 148.14, "scr": 251.17, "mcr": 113.03, "eligibleFunds": 372.09,
            "modules": [{"name": "Marché", "value": 108.73}, {"name": "Souscription Non-Vie", "value": 163.11}, {"name": "Vie", "value": 14.3}],
            "details": {"bscr": 250.48, "diversificationBenefit": -112.42, "mcrRatio": 238.08}}


def good_position(**kw):
    return {"id": "FR0001", "asset_class": "bond", "exposure": 1000, "rating": "AA", "duration": 5,
            "effective_date": "2020-01-01", "maturity_date": "2032-01-01", **kw}


def request_body(positions=None, solvency=None):
    return {"portfolio": {"valuation_date": "2026-12-31", "source": SOURCE, "positions": positions or [good_position()]},
            "solvency": solvency or SOLVENCY}


def fake_report(severity="low", **kw) -> RiskDiagnosisReport:
    return RiskDiagnosisReport(
        status={"low": "normal", "medium": "watchlist", "high": "anomaly_detected"}[severity], severity=severity,
        summary="Synthèse.", robustness_assessment="Robuste.",
        findings=[Finding(indicator="Ratio SCR", evidence="148.14 %", interpretation="Conforme.")],
        risk_drivers=[], orsa_recommendations=["Stress ORSA."], mitigation_plans=[], recommended_action="Surveiller.", **kw)


class FakeLLM:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, 0

    def invoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def client(monkeypatch):
    from main import app          # importing main does not load the RAG models: that happens in the lifespan only
    return TestClient(app)


def use_llm(monkeypatch, llm: FakeLLM, by_model: dict[str, FakeLLM] | None = None) -> FakeLLM:
    monkeypatch.setattr(analyst, "_structured_llm", lambda model: (by_model or {}).get(model, llm))
    return llm


# ----------------------------------------------------------------------------- routing


def test_clean_data_runs_the_analyst(client, monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(fake_report("low")))
    body = client.post("/api/pipeline/audit-and-risk", json=request_body()).json()
    assert body["pipeline_status"] == "COMPLETED" and body["diagnosis_source"] == "llm"
    assert body["data_quality"]["status"] == "PASSED" and llm.calls == 1
    assert body["diagnosis"]["severity"] == "low" and body["model"]


def test_blocked_data_never_calls_the_llm(client, monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(fake_report("low")))
    body = client.post("/api/pipeline/audit-and-risk", json=request_body([good_position(exposure=-10)])).json()
    assert body["pipeline_status"] == "BLOCKED" and body["diagnosis_source"] == "alert"
    assert body["data_quality"]["is_eligible_for_scr"] is False and llm.calls == 0
    assert body["diagnosis"]["severity"] == "high" and "EXPOSURE_NEGATIVE" in str(body["diagnosis"]["findings"])
    assert body["model"] is None


def test_warning_still_goes_to_the_analyst(client, monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(fake_report("low")))
    body = client.post("/api/pipeline/audit-and-risk", json=request_body([good_position(rating="ZZZ")])).json()
    assert body["data_quality"]["status"] == "WARNING" and body["pipeline_status"] == "COMPLETED" and llm.calls == 1


# ----------------------------------------------------------------------------- guardrails


@pytest.mark.parametrize("ratio, llm_severity, final", [
    (96.7, "low", "high"),         # breach can never come back reassuring
    (110.0, "low", "medium"),      # below the 130 % appetite
    (148.1, "high", "medium"),     # at most one level above what the numbers imply
    (148.1, "medium", "medium"),   # one level up is allowed (e.g. degraded data)
    (180.0, "low", "low"),
])
def test_severity_guardrail(ratio, llm_severity, final):
    scr = 100.0
    solvency = SolvencyMetrics(scr=scr, mcr=30, eligibleFunds=ratio, modules=[], details={"mcrRatio": 300})
    indicators = analyst.compute_indicators(solvency, None)
    report, notes = analyst.apply_guardrails(fake_report(llm_severity), indicators)
    assert report.severity == final
    assert report.status == analyst.STATUS_OF[final]
    assert bool(notes) == (final != llm_severity)


def test_mcr_breach_is_always_high():
    solvency = SolvencyMetrics(scr=100, mcr=50, eligibleFunds=200, modules=[], details={"mcrRatio": 80})
    assert analyst.expected_severity(analyst.compute_indicators(solvency, None)) == "high"


def test_indicators_are_precomputed_for_the_llm():
    solvency = SolvencyMetrics(**SOLVENCY)
    ind = analyst.compute_indicators(solvency, None)
    assert ind["regulatory_position"] == "COMFORTABLE"
    assert ind["surplus_over_100pct_meur"] == pytest.approx(120.92)
    assert ind["surplus_over_130pct_meur"] == pytest.approx(372.09 - 1.3 * 251.17, abs=0.01)    # indicators are rounded to 2 decimals
    assert ind["module_shares"][0]["module"] == "Souscription Non-Vie" and ind["module_shares"][0]["share_of_modules_pct"] == pytest.approx(57.0, abs=0.1)


def test_ratio_is_derived_when_absent():
    assert SolvencyMetrics(scr=200, mcr=50, eligibleFunds=300).ratio == 150.0


# ----------------------------------------------------------------------------- apostrophe repair


FILLER = " " + "La situation financière est détaillée dans les constats ci-dessous." * 12      # real reports are several KB


def report_with(summary: str, **kw) -> RiskDiagnosisReport:
    return fake_report("low", **kw).model_copy(update={"summary": summary + FILLER})


def test_apostrophes_are_restored_only_when_all_are_missing():
    broken = report_with("L organisme respecte d après les données ce qu il faut. Qu est-ce que c est ? Marge de 45.57 M EUR, soit 10 M euros.")
    fixed, repaired = analyst.restore_elisions(broken)
    assert repaired
    assert fixed.summary.startswith("L'organisme respecte d'après les données ce qu'il faut. Qu'est-ce que c'est ? Marge de 45.57 M EUR, soit 10 M euros.")


def test_text_that_already_has_apostrophes_is_left_alone():
    ok = report_with("L'organisme respecte l exigence.")      # one apostrophe present: nothing is touched
    same, repaired = analyst.restore_elisions(ok)
    assert not repaired and same.summary.startswith("L'organisme respecte l exigence.")


def test_degraded_text_is_retried_then_repaired_as_a_last_resort(client, monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(report_with("L organisme est solide.")))          # degraded on both attempts
    body = client.post("/api/pipeline/audit-and-risk", json=request_body()).json()
    assert llm.calls == 2                                                                  # one retry before repairing
    assert body["diagnosis"]["summary"].startswith("L'organisme est solide.") and any("apostrophes restaurées" in n for n in body["guardrail_notes"])


def test_a_good_second_attempt_replaces_a_degraded_first_one(client, monkeypatch):
    class Flaky(FakeLLM):
        def invoke(self, messages):
            self.calls += 1
            return report_with("L organisme est solide.") if self.calls == 1 else report_with("L'organisme est solide.")
    llm = use_llm(monkeypatch, Flaky())
    body = client.post("/api/pipeline/audit-and-risk", json=request_body()).json()
    assert llm.calls == 2 and body["diagnosis"]["summary"].startswith("L'organisme est solide.") and body["guardrail_notes"] == []


# ----------------------------------------------------------------------------- failures


def test_llm_outage_returns_partial_result_not_a_500(client, monkeypatch):
    use_llm(monkeypatch, FakeLLM(error=ConnectionError("fetch failed")))
    response = client.post("/api/pipeline/audit-and-risk", json=request_body())
    assert response.status_code == 200
    body = response.json()
    assert body["pipeline_status"] == "ANALYSIS_FAILED" and body["diagnosis"] is None
    assert body["data_quality"]["status"] == "PASSED" and "ConnectionError" in body["errors"][0]
    assert len(body["errors"]) == len(analyst.ANALYST_MODELS)      # every model was tried


def test_invalid_structured_output_is_retried_once_per_model(client, monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(result=None))
    body = client.post("/api/pipeline/audit-and-risk", json=request_body()).json()
    assert llm.calls == 2 * len(analyst.ANALYST_MODELS) and body["pipeline_status"] == "ANALYSIS_FAILED"


def test_fallback_model_takes_over_when_the_primary_is_down(client, monkeypatch):
    primary, fallback = analyst.ANALYST_MODELS[0], analyst.ANALYST_MODELS[1]
    down, up = FakeLLM(error=RuntimeError("503 UNAVAILABLE high demand")), FakeLLM(fake_report("low"))
    use_llm(monkeypatch, up, by_model={primary: down})
    body = client.post("/api/pipeline/audit-and-risk", json=request_body()).json()
    assert body["pipeline_status"] == "COMPLETED" and body["model"] == fallback
    assert down.calls == 1 and up.calls == 1 and "secours" in " ".join(body["guardrail_notes"])


def test_malformed_request_is_a_422(client):
    assert client.post("/api/pipeline/audit-and-risk", json={}).status_code == 422                       # portfolio is mandatory
    assert client.post("/api/pipeline/audit-and-risk", json={"portfolio": {"positions": "x"}}).status_code == 422
    audit_only = client.post("/api/pipeline/audit-and-risk", json={"portfolio": {"source": SOURCE, "valuation_date": "2026-12-31", "positions": [good_position()]}})
    assert audit_only.status_code == 200 and audit_only.json()["pipeline_status"] == "AUDITED"           # solvency is optional (gate before the SCR)
    assert client.post("/api/pipeline/audit-and-risk", json=request_body(solvency={**SOLVENCY, "scr": 0})).status_code == 422


def test_cors_allows_the_next_dev_server(client):
    response = client.options("/api/pipeline/audit-and-risk", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ----------------------------------------------------------------------------- investigator key


def test_investigator_has_its_own_key_with_fallback(monkeypatch):
    for name in analyst.KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert analyst.investigator_key_source() is None
    monkeypatch.setenv("GEMINI_API_KEY", "shared")
    assert analyst.investigator_key_source() == "GEMINI_API_KEY"
    monkeypatch.setenv("INVESTIGATOR_API_KEY", "dedicated")
    assert analyst.investigator_key_source() == "INVESTIGATOR_API_KEY"          # the dedicated key wins


def test_analyst_client_is_built_with_the_dedicated_key(monkeypatch):
    seen = {}

    class FakeChat:
        def __init__(self, **kw):
            seen.update(kw)

        def with_structured_output(self, schema):
            return "structured"

    monkeypatch.setenv("GEMINI_API_KEY", "shared")
    monkeypatch.setenv("INVESTIGATOR_API_KEY", "dedicated")
    monkeypatch.setattr(analyst, "ChatGoogleGenerativeAI", FakeChat)
    analyst._structured_llm.cache_clear()
    assert analyst._structured_llm("some-model") == "structured" and seen["google_api_key"] == "dedicated"
    analyst._structured_llm.cache_clear()
