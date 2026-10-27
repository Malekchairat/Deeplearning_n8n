"""Climate & ESG Stress Tester: a tool-calling agent (LangChain `create_agent`) that crosses the insured-property database with real
climate APIs and reports the share of the Non-Life SCR threatened by flood. Same key rotation and model fallback as the EIOPA agent."""
import time
from typing import Any, Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from climate_agent.stress import StressResult
from climate_agent.tools import ClimateContext, build_tools
from eiopa_agent import agent as shared             # key chain, models, quota cooldown and message helpers are common to the agents
from eiopa_agent.agent import ToolCallTrace

RECURSION_LIMIT = 25

SYSTEM_PROMPT = """Tu es l'agent « Climate & ESG Stress Tester » de SolvaRisk (Solvabilité II, ORSA : tests de résistance au changement climatique). Tu mesures l'exposition d'un portefeuille d'assurance habitation (Non-Vie) au risque d'inondation en croisant la base du portefeuille et des données climatiques réelles.

OUTILS
- query_portfolio_database : lit les biens assurés (ville, coordonnées GPS, valeur assurée, garantie inondation).
- fetch_geospatial_weather_api : interroge l'API climatique pour des coordonnées GPS et renvoie la classe de risque de chaque zone.
- compute_climate_stress : calcule le stress test sur tout le portefeuille.

PROCÉDURE OBLIGATOIRE
1. Interroge d'abord la base (une requête groupée par ville, avec ses coordonnées et la somme des valeurs assurées des contrats avec garantie inondation).
2. Appelle fetch_geospatial_weather_api pour les coordonnées de CHAQUE zone du portefeuille couvert, en une seule liste.
3. Appelle compute_climate_stress. Si des zones sont « non évaluées », interroge-les puis recalcule ; si l'API reste indisponible pour elles, dis-le.
4. Rédige le rapport.

RÈGLES STRICTES
- Tous les chiffres viennent des outils : tu n'en inventes, n'en recalcules et n'en additionnes aucun. Ne cite que des zones et des valeurs renvoyées par les outils.
- Réponds d'abord à la question posée (threatened_pct_of_scr : part du SCR Non-Vie menacée, pondérée par la probabilité d'au moins un jour de pluie dommageable sur l'horizon demandé), puis donne : la part située en zones à risque élevé (high_zone_pct), la perte attendue et sa part du SCR, les zones les plus exposées avec leur probabilité, la part non évaluée éventuelle.
- Précise toujours les limites : probabilités déduites d'un seul modèle climatique et d'un seuil de pluie supposé, SCR réparti au prorata de la valeur assurée, taux de dommage supposé, pas de modèle de catastrophe naturelle. Ce n'est pas une évaluation actuarielle calibrée.
- Ne prétends pas avoir utilisé une source que les outils ne t'ont pas renvoyée (par exemple Copernicus).
- Réponds en français, en phrases simples, sans mise en forme Markdown."""


class PropertyIn(BaseModel):
    city: str = Field(min_length=1, max_length=80)
    country: str = Field("FR", max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    property_type: str = Field("Habitation", max_length=60)
    sum_insured_keur: float = Field(gt=0, le=1e9)
    annual_premium_keur: float = Field(0, ge=0)
    flood_cover: int = Field(1, ge=0, le=1)


class ClimateRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    scr_nonlife: float = Field(gt=0, le=1_000_000, description="SCR Non-Vie to stress, in M EUR")
    horizon_years: int = Field(5, ge=1, le=25)
    properties: list[PropertyIn] | None = Field(None, min_length=1, max_length=5000, description="The client's own book; omitted = the demo portfolio")


class ClimateResponse(BaseModel):
    status: Literal["COMPLETED", "LLM_UNAVAILABLE", "DATA_UNAVAILABLE"]
    answer: str | None
    stress: StressResult | None
    portfolio_source: str = "demo"
    zones_fetched: int
    tool_calls: list[ToolCallTrace]
    model: str | None
    key_source: str | None = None
    errors: list[str] = Field(default_factory=list)


def _usable(ctx: ClimateContext) -> bool:
    return bool(ctx.hazards) and any(h["flood_hazard"] != "UNKNOWN" for h in ctx.hazards.values())


def run_agent(request: ClimateRequest, *, fetcher=None, llm_factory=None) -> ClimateResponse:
    kwargs: dict[str, Any] = {"fetcher": fetcher} if fetcher else {}
    ctx = ClimateContext(question=request.question, scr_nonlife=request.scr_nonlife, horizon_years=request.horizon_years,
                         properties=[p.model_dump() for p in request.properties] if request.properties else None, **kwargs)
    attempts = ([(m, n, k) for n, k in shared._api_keys() for m in shared.MODELS] if llm_factory is None else [(m, None, None) for m in shared.MODELS])
    if llm_factory is None and not attempts:
        attempts = [(shared.MODELS[0], None, None)]
    live = [a for a in attempts if shared._COOLDOWN.get((a[1] or "", a[0]), 0) <= time.time()]
    errors: list[str] = []
    for model, key_name, key in live or attempts:
        ctx.reset()
        label = f"{model} [{key_name}]" if key_name else model
        try:
            llm = llm_factory(model) if llm_factory else shared._make_llm(model, key)
            result = create_agent(llm, build_tools(ctx), system_prompt=SYSTEM_PROMPT).invoke(
                {"messages": [HumanMessage(content=request.question)]}, config={"recursion_limit": RECURSION_LIMIT})
        except Exception as exc:                 # noqa: BLE001 - quota / 5xx / tool error: next key or model
            if shared._is_quota_error(exc):
                shared._COOLDOWN[(key_name or "", model)] = time.time() + shared.QUOTA_COOLDOWN_S
            errors.append(f"{label} : {type(exc).__name__}: {str(exc)[:200]}")
            continue
        messages = result["messages"]
        answer = next((shared._text(m.content) for m in reversed(messages) if isinstance(m, AIMessage) and shared._text(m.content)), "")
        if not answer:
            errors.append(f"{label} : aucune réponse finale")
            continue
        if ctx.stress is None and _usable(ctx):
            ctx.compute()                       # the agent fetched but forgot to compute: the figures still come from the code
        return ClimateResponse(status="COMPLETED", portfolio_source="client" if request.properties else "demo", answer=answer, stress=ctx.stress, zones_fetched=len(ctx.hazards), tool_calls=shared._trace(messages),
                               model=model, key_source=key_name, errors=errors)
    # no model answered: the deterministic procedure (same API, same code) still gives the figures
    ctx.reset()
    ctx.deterministic_run()
    if not _usable(ctx):
        return ClimateResponse(status="DATA_UNAVAILABLE", portfolio_source="client" if request.properties else "demo", answer=None, stress=None, zones_fetched=len(ctx.hazards), tool_calls=[], model=None,
                               errors=errors + ["API climatique indisponible : aucune zone n'a pu être évaluée."])
    return ClimateResponse(status="LLM_UNAVAILABLE", portfolio_source="client" if request.properties else "demo", answer=None, stress=ctx.stress, zones_fetched=len(ctx.hazards), tool_calls=[], model=None, errors=errors)
