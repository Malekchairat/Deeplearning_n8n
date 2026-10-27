"""Tests of the Climate & ESG Stress Tester (no network: scripted chat model, stubbed weather API).
Run: python -m pytest climate_agent/tests -q   (from rag_service/)"""
from datetime import date

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from climate_agent import agent as climate_agent_module
from climate_agent import portfolio_db, weather
from climate_agent.agent import ClimateRequest, run_agent
from climate_agent.portfolio_db import CITIES, SECONDARY_WITHOUT_COVER, query_portfolio
from climate_agent.stress import compute_stress
from climate_agent.tools import ClimateContext, build_tools

HIGH_CITIES = {"Marseille", "Nice"}
SCR_NL = 400.0


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio_db, "DB_PATH", tmp_path / "portfolio.db")
    monkeypatch.setattr(weather, "CACHE_DIR", tmp_path / "climate_cache")            # never write test data into the real cache


P_HIGH, P_LOW = 0.6, 0.05                      # annual probabilities of the stubbed zones


def stub_fetcher(lat, lon, horizon):
    city = next(c for c, la, lo, _ in CITIES if weather.zone_key(la, lo) == weather.zone_key(lat, lon))
    p = P_HIGH if city in HIGH_CITIES else P_LOW
    return {"zone": weather.zone_key(lat, lon), "latitude": lat, "longitude": lon, "horizon_years": horizon, "rx1_mm": 60.0, "annual_event_prob": p,
            "horizon_event_prob": weather.horizon_prob(p, horizon), "river_discharge_ratio": 2.0, "flood_hazard": weather.classify(p), "errors": [], "source": "stub"}


def expected_covered_and_high():
    si = lambda w, cover_secondary: 120_000 * w + 200_000 * w                       # noqa: E731 - individual + collective blocks, k EUR
    covered = sum(si(w, 0) for _c, _la, _lo, w in CITIES) / 1000
    high = sum(si(w, 0) for c, _la, _lo, w in CITIES if c in HIGH_CITIES) / 1000
    return covered, high


# ----------------------------------------------------------------------------- database


def test_database_is_seeded_and_read_only():
    rows, truncated = query_portfolio("SELECT city, SUM(sum_insured_keur) AS si FROM insured_properties GROUP BY city")
    assert len(rows) == len(CITIES) and not truncated
    for bad in ("DELETE FROM insured_properties", "SELECT 1; DROP TABLE insured_properties", "SELECT * FROM insured_properties -- x",
                "PRAGMA table_info(insured_properties)", "SELECT * FROM insured_properties; SELECT 1"):
        with pytest.raises(ValueError):
            query_portfolio(bad)
    assert len(portfolio_db.all_properties()) == 2 * len(CITIES) + len(SECONDARY_WITHOUT_COVER)    # the table was not modified


def test_invalid_sql_is_a_clean_error():
    with pytest.raises(ValueError, match="invalide"):
        query_portfolio("SELECT nope FROM insured_properties")


# ----------------------------------------------------------------------------- weather


def climate_payload(rain_by_year, wind=40.0):
    times, rain = [], []
    for year, peak in rain_by_year.items():
        times += [f"{year}-01-01", f"{year}-06-01"]
        rain += [peak, 1.0]
    return {"daily": {"time": times, "precipitation_sum": rain, "wind_speed_10m_max": [wind] * len(times)}}


def test_hazard_from_the_two_apis():
    def getter(url, params):
        if url == weather.CLIMATE_URL:
            return climate_payload({2027: 70.0, 2028: 50.0, 2029: 10.0, 2030: 10.0})
        return {"daily": {"river_discharge_mean": [1.0, 1.0], "river_discharge_max": [2.0, 3.0]}}
    h = weather.fetch_hazard(43.3, 5.4, 5, getter=getter, today=date(2026, 10, 27))
    assert h["rx1_mm"] == 35.0 and h["annual_event_prob"] == 0.5 and h["river_discharge_ratio"] == 3.0 and h["flood_hazard"] == "HIGH"
    assert h["horizon_event_prob"] == pytest.approx(1 - 0.5 ** 5, abs=1e-4) and h["errors"] == []


def test_the_horizon_moves_the_probability():
    assert weather.horizon_prob(0.2, 3) == pytest.approx(0.488) and weather.horizon_prob(0.2, 10) == pytest.approx(0.8926, abs=1e-4)
    assert weather.horizon_prob(None, 5) is None


def test_classification_rules():
    assert weather.classify(0.1) == "LOW" and weather.classify(0.3) == "MEDIUM" and weather.classify(0.6) == "HIGH" and weather.classify(None) == "UNKNOWN"
    assert weather.effective_prob(0.4, 15) == 0.5 and weather.effective_prob(0.4, 5) == 0.4 and weather.effective_prob(0.9, 15) == 1.0


def test_a_failing_api_is_reported_never_invented():
    def down(url, params):
        raise TimeoutError("timed out")
    h = weather.fetch_hazard(48.8, 2.3, 5, getter=down)
    assert h["flood_hazard"] == "UNKNOWN" and len(h["errors"]) == 2 and h["annual_event_prob"] is None and h["horizon_event_prob"] is None


def test_rivers_down_keeps_the_climate_result_and_climate_down_is_unknown():
    def rivers_down(url, params):
        if url == weather.FLOOD_URL:
            raise ConnectionError("down")
        return climate_payload({2027: 45.0})
    h = weather.fetch_hazard(48.8, 2.3, 5, getter=rivers_down)
    assert h["flood_hazard"] == "HIGH" and h["river_discharge_ratio"] is None and len(h["errors"]) == 1

    def climate_down(url, params):
        if url == weather.CLIMATE_URL:
            raise ConnectionError("down")
        return {"daily": {"river_discharge_mean": [1.0], "river_discharge_max": [50.0]}}
    assert weather.fetch_hazard(48.8, 2.3, 5, getter=climate_down)["flood_hazard"] == "UNKNOWN"          # the rivers alone are not a 5-year signal


# ----------------------------------------------------------------------------- stress (deterministic)


def test_stress_numbers():
    ctx = ClimateContext(question="q", scr_nonlife=SCR_NL, fetcher=stub_fetcher)
    r = ctx.deterministic_run()
    covered, high = expected_covered_and_high()
    ph, pl = weather.horizon_prob(P_HIGH, 5), weather.horizon_prob(P_LOW, 5)
    weighted = high * ph + (covered - high) * pl
    assert r.covered_sum_insured_meur == pytest.approx(covered, abs=0.01)
    assert r.threatened_pct_of_scr == pytest.approx(100 * weighted / covered, abs=0.01)
    assert r.high_zone_pct == pytest.approx(100 * high / covered, abs=0.01)
    assert r.threatened_scr_meur == pytest.approx(SCR_NL * weighted / covered, abs=0.01)
    assert r.scenario_loss_meur == pytest.approx(weighted * 0.02, abs=0.01)
    assert r.excluded_no_cover_meur > 0 and r.unassessed_sum_insured_meur == 0
    assert {z.hazard for z in r.zones if z.cities[0] in HIGH_CITIES} == {"HIGH"}


def test_a_longer_horizon_and_another_portfolio_change_the_result():
    short = ClimateContext(question="q", scr_nonlife=SCR_NL, horizon_years=1, fetcher=stub_fetcher).deterministic_run()
    long = ClimateContext(question="q", scr_nonlife=SCR_NL, horizon_years=10, fetcher=stub_fetcher).deterministic_run()
    assert short.threatened_pct_of_scr < long.threatened_pct_of_scr
    other = ClimateContext(question="q", scr_nonlife=SCR_NL, fetcher=stub_fetcher, properties=own_book()).deterministic_run()
    assert other.threatened_pct_of_scr != ClimateContext(question="q", scr_nonlife=SCR_NL, fetcher=stub_fetcher).deterministic_run().threatened_pct_of_scr


def test_policies_without_cover_do_not_count():
    props = [dict(city="A", latitude=1.0, longitude=1.0, sum_insured_keur=100_000, flood_cover=1),
             dict(city="B", latitude=2.0, longitude=2.0, sum_insured_keur=900_000, flood_cover=0)]
    r = compute_stress(props, {"1.0,1.0": {"flood_hazard": "HIGH", "horizon_event_prob": 1.0}}, 100.0, 5)
    assert r.covered_sum_insured_meur == 100 and r.threatened_pct_of_scr == 100 and r.excluded_no_cover_meur == 900


def test_unfetched_zones_are_unassessed_not_assumed_safe():
    props = [dict(city="A", latitude=1.0, longitude=1.0, sum_insured_keur=100_000, flood_cover=1),
             dict(city="B", latitude=2.0, longitude=2.0, sum_insured_keur=300_000, flood_cover=1)]
    r = compute_stress(props, {"1.0,1.0": {"flood_hazard": "HIGH", "horizon_event_prob": 1.0}}, 100.0, 5)
    assert r.unassessed_sum_insured_meur == 300 and r.threatened_pct_of_scr == 25.0


# ----------------------------------------------------------------------------- agent


class ScriptedChat(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args=None, id_="1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id_}])


def all_points():
    return [{"latitude": la, "longitude": lo} for _c, la, lo, _w in CITIES]


def script():
    return iter([call("query_portfolio_database", {"sql": "SELECT city, latitude, longitude FROM insured_properties GROUP BY city"}, "a"),
                 call("fetch_geospatial_weather_api", {"points": all_points()}, "b"), call("compute_climate_stress", {}, "c"),
                 AIMessage(content="Une part du SCR Non-Vie est menacée par les inondations.")])


def test_the_agent_queries_then_fetches_then_computes():
    response = run_agent(ClimateRequest(question="Quel % du SCR Non-Vie est menacé ?", scr_nonlife=SCR_NL), fetcher=stub_fetcher,
                         llm_factory=lambda model: ScriptedChat(messages=script()))
    assert response.status == "COMPLETED" and response.zones_fetched == len(CITIES)
    assert [t.tool for t in response.tool_calls] == ["query_portfolio_database", "fetch_geospatial_weather_api", "compute_climate_stress"]
    covered, high = expected_covered_and_high()
    assert response.stress.high_zone_pct == pytest.approx(100 * high / covered, abs=0.01)


def test_weather_tool_refuses_before_the_database_was_read():
    ctx = ClimateContext(question="q", scr_nonlife=SCR_NL, fetcher=stub_fetcher)
    tools = {t.name: t for t in build_tools(ctx)}
    assert "d'abord" in tools["fetch_geospatial_weather_api"].invoke({"points": [{"latitude": 43.3, "longitude": 5.4}]})
    assert "d'abord" in tools["compute_climate_stress"].invoke({})
    assert "refusée" in tools["query_portfolio_database"].invoke({"sql": "DROP TABLE insured_properties"})


def test_figures_come_from_the_code_even_if_the_agent_forgets_to_compute():
    steps = iter([call("query_portfolio_database", {"sql": "SELECT city FROM insured_properties"}, "a"),
                  call("fetch_geospatial_weather_api", {"points": all_points()}, "b"), AIMessage(content="Rapport.")])
    r = run_agent(ClimateRequest(question="Analyse le portefeuille", scr_nonlife=SCR_NL), fetcher=stub_fetcher, llm_factory=lambda m: ScriptedChat(messages=steps))
    assert r.status == "COMPLETED" and r.stress is not None


def test_llm_down_still_returns_the_deterministic_stress(monkeypatch):
    def boom(model):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    r = run_agent(ClimateRequest(question="Analyse le portefeuille", scr_nonlife=SCR_NL), fetcher=stub_fetcher, llm_factory=boom)
    assert r.status == "LLM_UNAVAILABLE" and r.answer is None and r.stress.threatened_pct_of_scr > 0
    assert len(r.errors) == len(climate_agent_module.shared.MODELS)


def test_climate_api_down_is_reported_as_data_unavailable():
    def down(lat, lon, horizon):
        return {"zone": weather.zone_key(lat, lon), "flood_hazard": "UNKNOWN", "errors": ["API climat : down"]}
    def boom(model):
        raise RuntimeError("no llm")
    r = run_agent(ClimateRequest(question="Analyse le portefeuille", scr_nonlife=SCR_NL), fetcher=down, llm_factory=boom)
    assert r.status == "DATA_UNAVAILABLE" and r.stress is None


def test_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    monkeypatch.setattr(climate_agent_module.shared, "_make_llm", lambda model, key=None: ScriptedChat(messages=script()))
    monkeypatch.setattr(weather, "_get", lambda url, params: climate_payload({2027: 70.0}) if url == weather.CLIMATE_URL
                        else {"daily": {"river_discharge_mean": [1.0], "river_discharge_max": [1.0]}})
    monkeypatch.setattr(climate_agent_module.shared, "_api_keys", lambda: [("TEST_KEY", "k")])
    client = TestClient(app)
    assert client.post("/api/climate-agent/run", json={"question": "ok", "scr_nonlife": -1}).status_code == 422
    body = client.post("/api/climate-agent/run", json={"question": "Quel % du SCR Non-Vie est menacé ?", "scr_nonlife": SCR_NL}).json()
    assert body["status"] in ("COMPLETED", "LLM_UNAVAILABLE") and body["key_source"] in ("TEST_KEY", None)


# ----------------------------------------------------------------------------- client portfolio


def own_book():
    from climate_agent.agent import PropertyIn
    return [PropertyIn(**p).model_dump() for p in _raw_book()]


def _raw_book():
    return [{"city": "Marseille", "latitude": 43.2965, "longitude": 5.3698, "sum_insured_keur": 100_000, "flood_cover": 1},
            {"city": "Paris", "latitude": 48.8566, "longitude": 2.3522, "sum_insured_keur": 300_000, "flood_cover": 1}]


def test_the_clients_own_portfolio_replaces_the_demo_book():
    from climate_agent.agent import PropertyIn
    props = [PropertyIn(**p).model_dump() for p in own_book()]
    rows, _ = query_portfolio("SELECT city FROM insured_properties", props)
    assert {r["city"] for r in rows} == {"Marseille", "Paris"}
    with pytest.raises(ValueError):
        query_portfolio("DELETE FROM insured_properties", props)
    r = run_agent(ClimateRequest(question="Analyse mon portefeuille", scr_nonlife=SCR_NL, properties=_raw_book()), fetcher=stub_fetcher,
                  llm_factory=lambda m: (_ for _ in ()).throw(RuntimeError("no llm")))
    assert r.portfolio_source == "client" and r.stress.covered_sum_insured_meur == 400 and r.stress.high_zone_pct == 25.0   # Marseille = 100 / 400


def test_the_percentage_is_independent_of_the_scr_but_the_amounts_scale():
    a = ClimateContext(question="q", scr_nonlife=100.0, fetcher=stub_fetcher, properties=own_book()).deterministic_run()
    b = ClimateContext(question="q", scr_nonlife=200.0, fetcher=stub_fetcher, properties=own_book()).deterministic_run()
    assert a.threatened_pct_of_scr == b.threatened_pct_of_scr and b.threatened_scr_meur == pytest.approx(2 * a.threatened_scr_meur, abs=0.02)
    assert b.scenario_loss_pct_of_scr == pytest.approx(a.scenario_loss_pct_of_scr / 2, abs=0.01)
