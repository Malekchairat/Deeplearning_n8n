"""Unit tests of Agent 1 (deterministic rules). Run: pytest pipeline/tests -q   (from rag_service/)"""
import pytest

from pipeline.agents.data_quality_agent import audit_portfolio, data_quality_node
from pipeline.schemas import PortfolioPayload

SOURCE = {"system": "Core-SI", "extraction_date": "2026-12-31", "reference": "LOT-0042"}


def bond(**kw):
    return {"id": "FR0001", "asset_class": "bond", "exposure": 1000.0, "rating": "AA-", "duration": 4.2,
            "effective_date": "2020-01-15", "maturity_date": "2031-01-15", **kw}


def run(positions, **portfolio):
    payload = {"valuation_date": "2026-12-31", "source": SOURCE, "positions": positions, **portfolio}
    return audit_portfolio(PortfolioPayload(**payload))


def codes(report):
    return {a.code for a in report.anomalies}


def test_clean_portfolio_passes():
    report = run([bond(), bond(id="FR0002", rating="Baa2", duration=7), {"id": "EQ1", "asset_class": "equity", "exposure": 250}])
    assert report.status == "PASSED" and report.is_eligible_for_scr
    assert report.quality_score == 100.0 and report.anomalies == []


@pytest.mark.parametrize("override, code, severity", [
    ({"exposure": -5}, "EXPOSURE_NEGATIVE", "CRITICAL"),
    ({"exposure": "abc"}, "EXPOSURE_NOT_NUMERIC", "CRITICAL"),
    ({"exposure": None}, "EXPOSURE_MISSING", "CRITICAL"),
    ({"id": ""}, "ID_MISSING", "CRITICAL"),
    ({"duration": -1.5}, "DURATION_NEGATIVE", "CRITICAL"),
    ({"maturity_date": "2019-01-01"}, "MATURITY_BEFORE_EFFECTIVE", "CRITICAL"),
])
def test_critical_rules_block_the_pipeline(override, code, severity):
    report = run([bond(**override)])
    assert code in codes(report)
    assert next(a for a in report.anomalies if a.code == code).severity.value == severity
    assert report.status == "BLOCKED" and report.is_eligible_for_scr is False


@pytest.mark.parametrize("override, code", [
    ({"rating": "ZZZ"}, "RATING_INVALID"),
    ({"rating": None}, "RATING_MISSING"),
    ({"duration": None}, "DURATION_MISSING"),
    ({"duration": 250}, "DURATION_IMPLAUSIBLE"),
    ({"maturity_date": "2026-06-30"}, "MATURITY_BEFORE_VALUATION"),
    ({"maturity_date": "not a date"}, "MATURITY_DATE_INVALID"),
])
def test_medium_rules_warn_but_stay_eligible(override, code):
    report = run([bond(**override)])
    assert code in codes(report)
    assert report.status == "WARNING" and report.is_eligible_for_scr is True


def test_recognised_rating_scales():
    for rating in ("AAA", "bbb+", "Aa3", "BAA1", "Caa2", "CQS 3", "D"):
        assert "RATING_INVALID" not in codes(run([bond(rating=rating)])), rating
    assert "RATING_UNRATED" in codes(run([bond(rating="NR")]))


def test_french_column_names_and_decimal_commas():
    report = run([{"identifiant": "P1", "classe_actif": "obligation", "valeur_marche": "1 234,5", "notation": "A+", "duree": "3,5",
                   "date_effet": "15/01/2020", "date_echeance": "15/01/2030"}])
    assert report.status == "PASSED", report.anomalies


def test_insurance_lines_need_premiums_and_claims():
    report = run([{"id": "NV1", "asset_class": "non_vie", "premium": 120.0}])
    assert codes(report) == {"CLAIMS_MISSING"} and report.status == "WARNING"
    assert "PREMIUM_NEGATIVE" in codes(run([{"id": "NV2", "asset_class": "iard", "premium": -3, "claims": 1}]))


def test_duplicate_ids_are_flagged_once():
    report = run([bond(), bond(), bond()])
    assert report.counts_by_code["ID_DUPLICATE"] == 1 and report.status == "WARNING"


def test_traceability_and_empty_portfolio():
    report = run([], source=None)
    assert {"EMPTY_PORTFOLIO", "SOURCE_MISSING"} <= codes(report)
    assert report.status == "BLOCKED" and report.quality_score <= 100 - 20 - 5


def test_missing_valuation_date_is_a_warning():
    report = run([bond()], valuation_date=None)
    assert codes(report) == {"VALUATION_DATE_MISSING"} and report.status == "WARNING"


def test_score_scales_with_error_rate_but_a_critical_row_always_blocks():
    many = [bond(id=f"B{i}") for i in range(999)] + [bond(id="BAD", exposure=-1)]
    report = run(many)
    assert report.status == "BLOCKED" and report.quality_score > 99.0     # 1 bad row in 1000: high score, still blocked


def test_listing_is_capped_but_counts_are_complete():
    report = run([bond(id=f"B{i}", exposure=-1) for i in range(500)])
    assert len(report.anomalies) == 200 and report.anomalies_truncated == 300
    assert report.counts_by_severity["CRITICAL"] == 500


def test_node_writes_data_quality_into_state():
    state = {"portfolio": PortfolioPayload(valuation_date="2026-12-31", source=SOURCE, positions=[bond()])}
    assert data_quality_node(state)["data_quality"].status == "PASSED"


# ----------------------------------------------------------------------------- QRT mode (aggregated tables, what the app holds)


def qrt(rows, **kw):
    return run([{"_sheet": "S2501_SCR", **r} for r in rows], **kw)


def test_legitimate_negative_qrt_lines_are_not_flagged():
    report = qrt([
        {"Code EIOPA": "S.25.01-R0010", "Rubrique": "Risque de marché", "Brut (C0040)": 108.7, "Net (C0030)": 108.7, "Unité": "M€"},
        {"Code EIOPA": "S.25.01-R0060", "Rubrique": "Diversification", "Brut (C0040)": -112.4, "Net (C0030)": -112.4, "Unité": "M€"},
        {"Code EIOPA": "S.25.01-R0140", "Rubrique": "Absorption par les provisions techniques", "Brut (C0040)": -4.2, "Net (C0030)": -4.2, "Unité": "M€"},
        {"Code EIOPA": "S.25.01-R0150", "Rubrique": "Capacité d'absorption – impôts différés", "Net (C0030)": -18.5, "Unité": "M€"},
    ])
    assert report.status == "PASSED" and report.is_eligible_for_scr, report.anomalies


def test_negative_risk_module_or_non_numeric_amount_blocks():
    report = qrt([{"code": "S.25.01-R0010", "libelle": "Risque de marché", "valeur": -5, "unite": "M€"},
                  {"code": "S.25.01-R0100", "libelle": "SCR de base", "valeur": "n/a", "unite": "M€"}])
    assert {"QRT_VALUE_NEGATIVE", "QRT_VALUE_NOT_NUMERIC"} <= codes(report)
    assert report.status == "BLOCKED" and not report.is_eligible_for_scr


def test_qrt_completeness_and_duplicates():
    report = qrt([{"code": "S.25.01-R0200", "libelle": "SCR", "valeur": 100, "unite": "M€"},
                  {"code": "S.25.01-R0200", "libelle": "SCR", "valeur": 120, "unite": "M€"},      # same line, different amount
                  {"code": "S.25.01-R0010", "libelle": "", "valeur": 5, "unite": "M€"},
                  {"code": "S.28.01-R0310", "libelle": "MCR", "valeur": None, "unite": "kg"}])
    assert {"QRT_DUPLICATE_LINE", "QRT_LABEL_MISSING", "QRT_VALUE_MISSING", "QRT_UNIT_UNKNOWN"} <= codes(report)
    assert report.status == "WARNING" and report.is_eligible_for_scr


def test_qrt_label_columns_are_matched_by_prefix():
    report = qrt([{"Code EIOPA": "S.23.01-R0500", "Rubrique / Élément Solvabilité II": "Tier 1 non restreint", "Montant Net (C0030)": 325, "Unité": "M€"}])
    assert report.status == "PASSED"


def test_position_data_is_not_mistaken_for_qrt():
    report = run([{"id": "P1", "libelle": "Obligation X", "exposure": -5, "asset_class": "bond", "rating": "AA", "duration": 3}])
    assert "EXPOSURE_NEGATIVE" in codes(report) and not any(c.startswith("QRT_") for c in codes(report))


def test_audit_only_graph_stops_without_solvency(monkeypatch):
    import asyncio
    from pipeline.graph import run_pipeline
    from pipeline.schemas import PipelineRequest
    result = asyncio.run(run_pipeline(PipelineRequest(portfolio={"source": SOURCE, "valuation_date": "2026-12-31", "positions": [bond()]})))
    assert result.pipeline_status == "AUDITED" and result.diagnosis is None and result.data_quality.status == "PASSED"
    blocked = asyncio.run(run_pipeline(PipelineRequest(portfolio={"source": SOURCE, "positions": [bond(exposure=-1)]})))
    assert blocked.pipeline_status == "BLOCKED" and blocked.diagnosis_source == "alert"


# ----------------------------------------------------------------------------- not a QRT / prudential summaries


def test_a_table_without_any_qrt_code_is_blocked_before_the_engine():
    """The 'Rapport Prudentiel' case: balance-sheet lines read by label would become fake risk charges."""
    report = qrt([{"Rubrique": "Actions cotées et Participations stratégiques", "Valeur": 3692, "Unité": "M€"},
                  {"Rubrique": "Best Estimate Passif Non-Vie (Sinistres et Primes)", "Valeur": 5800, "Unité": "M€"},
                  {"Rubrique": "SCR Global Solvabilité II", "Valeur": 2150, "Unité": "M€"}])
    assert "QRT_NO_TEMPLATE_CODES" in codes(report)
    assert report.status == "BLOCKED" and not report.is_eligible_for_scr


def summary(**figures):
    return audit_portfolio(PortfolioPayload(document_kind="summary", source=SOURCE, positions=[figures]))


def test_consistent_published_figures_pass_with_the_mcr_corridor_flagged():
    report = summary(scr=2150, eligible_funds=4720, mcr=983, ratio_scr_pct=219.5, ratio_mcr_pct=480.2)
    found = codes(report)
    assert "SUMMARY_MCR_CORRIDOR" in found                     # 983 / 2150 = 45.7 % of the SCR
    assert "SUMMARY_NOT_RECOMPUTED" in found and "SUMMARY_RATIO_MISMATCH" not in found and "SUMMARY_MCR_RATIO_MISMATCH" not in found
    assert report.status == "WARNING" and report.is_eligible_for_scr
    assert not any(c.startswith("QRT_") for c in found)        # published figures are not audited as QRT rows


def test_published_figures_that_contradict_each_other_are_flagged():
    report = summary(scr=2150, eligible_funds=4720, mcr=700, ratio_scr_pct=300, ratio_mcr_pct=900)
    assert {"SUMMARY_RATIO_MISMATCH", "SUMMARY_MCR_RATIO_MISMATCH"} <= codes(report) and report.status == "WARNING"


def test_a_summary_without_scr_or_funds_is_blocked():
    report = summary(mcr=983, ratio_scr_pct=219.5)
    assert {"SUMMARY_SCR_MISSING", "SUMMARY_FUNDS_MISSING"} <= codes(report) and report.status == "BLOCKED"


def test_summary_checks_are_scored_at_document_level():
    """One record only: a single remark must not halve the score as a per-row penalty would."""
    report = summary(scr=2150, eligible_funds=4720, mcr=983, ratio_scr_pct=219.5, ratio_mcr_pct=480.2)
    assert report.quality_score >= 90 and all(a.row is None for a in report.anomalies)
