"""The three tools of the Climate & ESG Stress Tester, bound to ONE request (a ClimateContext).

The model decides what to query and which zones to fetch; every figure (hazard class, exposure, % of SCR) is computed by the code.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from climate_agent import portfolio_db
from climate_agent.stress import StressResult, compute_stress
from climate_agent.weather import fetch_hazard, zone_key

MAX_POINTS = 25
Fetcher = Callable[[float, float, int], dict[str, Any]]


class Point(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


@dataclass
class ClimateContext:
    question: str
    scr_nonlife: float
    horizon_years: int = 5
    fetcher: Fetcher = fetch_hazard
    properties: list[dict[str, Any]] | None = None                        # the client's own portfolio; None = the seeded demo book
    hazards: dict[str, dict[str, Any]] = field(default_factory=dict)      # zone -> hazard actually returned by the API
    queried: bool = False
    stress: StressResult | None = None

    def reset(self) -> None:
        self.hazards, self.queried, self.stress = {}, False, None

    def fetch_zones(self, points: list[tuple[float, float]]) -> list[dict[str, Any]]:
        unique = list({zone_key(la, lo): (la, lo) for la, lo in points}.values())[:MAX_POINTS]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda p: self.fetcher(p[0], p[1], self.horizon_years), unique))
        for h in results:
            self.hazards[h["zone"]] = h
        return results

    def compute(self) -> StressResult:
        self.stress = compute_stress(portfolio_db.all_properties(self.properties), self.hazards, self.scr_nonlife, self.horizon_years)
        return self.stress

    def deterministic_run(self) -> StressResult:
        """The whole procedure without the model (used when no LLM answers)."""
        self.fetch_zones([(p["latitude"], p["longitude"]) for p in portfolio_db.all_properties(self.properties) if p["flood_cover"]])
        return self.compute()


def build_tools(ctx: ClimateContext) -> list:
    @tool
    def query_portfolio_database(sql: str) -> str:
        """Interroge la base du portefeuille d'assurance habitation (lecture seule, UNE requête SELECT). Table insured_properties :
        id, city, country, latitude, longitude, property_type ('Habitation individuelle' | 'Habitation collective' | 'Résidence secondaire'),
        sum_insured_keur (valeur assurée, k€), annual_premium_keur, flood_cover (1 = garantie inondation / catastrophes naturelles, 0 = non).
        Sert à connaître les localisations (ville, latitude, longitude) et les valeurs assurées. Maximum 200 lignes : utilise GROUP BY."""
        try:
            rows, truncated = portfolio_db.query_portfolio(sql, ctx.properties)
        except ValueError as exc:
            return f"Requête refusée : {exc}"
        ctx.queried = True
        return json.dumps({"lignes": len(rows), "tronque": truncated, "resultat": rows}, ensure_ascii=False)

    @tool
    def fetch_geospatial_weather_api(points: list[Point]) -> str:
        """Interroge l'API climatique réelle Open-Meteo (modèle climatique à haute résolution + débits des rivières GloFAS) pour une liste
        de coordonnées GPS (une par zone à risque, 25 maximum, les points voisins d'environ 10 km sont regroupés). Renvoie, par zone :
        probabilité annuelle (annual_event_prob) et sur l'horizon (horizon_event_prob) d'au moins un jour de pluie dommageable, pluie journalière
        extrême moyenne (rx1_mm), ratio de débit des rivières et classe de risque inondation (HIGH / MEDIUM / LOW / UNKNOWN), le tout calculé par le code. UNKNOWN = API indisponible : ne rien en déduire."""
        if not ctx.queried:
            return "Interroge d'abord la base du portefeuille (query_portfolio_database) pour connaître les localisations."
        results = ctx.fetch_zones([(p.latitude, p.longitude) for p in points])
        return json.dumps([{k: h[k] for k in ("zone", "flood_hazard", "annual_event_prob", "horizon_event_prob", "rx1_mm", "river_discharge_ratio", "errors")}
                           for h in results], ensure_ascii=False)

    @tool
    def compute_climate_stress() -> str:
        """Calcule, en Python, le stress test inondation sur TOUT le portefeuille à partir des zones déjà interrogées : % de la valeur assurée
        (donc du SCR Non-Vie) menacée, pondérée par la probabilité d'épisode de chaque zone sur l'horizon, part en zones à risque élevé, perte attendue et sa part du SCR Non-Vie. À appeler une fois les zones récupérées ;
        le résultat liste les zones non évaluées (à interroger avant de conclure). Ne recalcule ni ne modifie aucun chiffre."""
        if not ctx.hazards:
            return "Aucune zone n'a encore été interrogée : appelle d'abord fetch_geospatial_weather_api."
        r = ctx.compute()
        missing = [z.zone for z in r.zones if z.hazard == "UNKNOWN"]
        out = r.model_dump(exclude={"zones"})
        out["zones"] = [{"zone": z.zone, "villes": z.cities, "classe": z.hazard, "valeur_assuree_meur": z.sum_insured_meur} for z in r.zones]
        out["zones_non_evaluees"] = missing
        return json.dumps(out, ensure_ascii=False)

    return [query_portfolio_database, fetch_geospatial_weather_api, compute_climate_stress]
