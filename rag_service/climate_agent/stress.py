"""The flood stress test, in Python: the model decides what to fetch, this code does every sum.

Method (stated to the user with the result):
- scope: policies WITH flood / natural-catastrophe cover (the others cannot cost the insurer anything);
- each zone has the probability P of at least one damaging rain day over the horizon (weather.horizon_prob: it grows with the horizon and differs by zone);
- share of the SCR Non-Vie threatened = sum(insured value x P) / covered insured value (the SCR is allocated pro rata to insured value: a simplification);
- expected loss over the horizon = sum(insured value x P) x DAMAGE_RATIO (an ASSUMPTION, not calibrated on claims history).
"""
from typing import Any

from pydantic import BaseModel, Field

DAMAGE_RATIO = 0.02
LABEL = {"HIGH": "Élevé", "MEDIUM": "Modéré", "LOW": "Faible", "UNKNOWN": "Non évalué"}
ORDER = ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]


class ClassExposure(BaseModel):
    hazard: str
    label: str
    zones: int
    sum_insured_meur: float
    share_pct: float = Field(description="Share of the covered sum insured")


class ZoneExposure(BaseModel):
    zone: str
    cities: list[str]
    hazard: str
    label: str
    sum_insured_meur: float
    annual_event_prob: float | None
    horizon_event_prob: float | None
    rx1_mm: float | None
    river_discharge_ratio: float | None


class StressResult(BaseModel):
    horizon_years: int
    scr_nonlife_meur: float
    covered_sum_insured_meur: float
    excluded_no_cover_meur: float
    unassessed_sum_insured_meur: float
    by_class: list[ClassExposure]
    threatened_pct_of_scr: float = Field(description="Probability-weighted share of the covered insured value hit by at least one damaging rain day over the horizon, in %")
    threatened_scr_meur: float
    high_zone_pct: float = Field(description="Share of the covered insured value located in HIGH zones, in %")
    elevated_or_moderate_pct: float
    scenario_loss_meur: float = Field(description="Expected loss over the horizon")
    scenario_loss_pct_of_scr: float
    damage_ratio: float
    zones: list[ZoneExposure]
    method: str


def compute_stress(properties: list[dict[str, Any]], hazards: dict[str, dict[str, Any]], scr_nonlife: float, horizon_years: int) -> StressResult:
    from climate_agent.weather import zone_key
    zones: dict[str, dict[str, Any]] = {}
    for p in properties:
        if not p["flood_cover"]:
            continue
        entry = zones.setdefault(zone_key(p["latitude"], p["longitude"]), {"cities": set(), "si": 0.0})
        entry["cities"].add(p["city"])
        entry["si"] += p["sum_insured_keur"] / 1000
    total = sum(z["si"] for z in zones.values())
    by_hazard: dict[str, list[float]] = {}
    rows, weighted = [], 0.0
    for z, e in zones.items():
        h = hazards.get(z, {})
        hazard, prob = h.get("flood_hazard", "UNKNOWN"), h.get("horizon_event_prob")
        by_hazard.setdefault(hazard, []).append(e["si"])
        weighted += e["si"] * (prob or 0.0)
        rows.append(ZoneExposure(zone=z, cities=sorted(e["cities"]), hazard=hazard, label=LABEL[hazard], sum_insured_meur=round(e["si"], 2),
                                 annual_event_prob=h.get("annual_event_prob"), horizon_event_prob=prob, rx1_mm=h.get("rx1_mm"),
                                 river_discharge_ratio=h.get("river_discharge_ratio")))
    rows.sort(key=lambda r: (ORDER.index(r.hazard), -(r.horizon_event_prob or 0), -r.sum_insured_meur))
    pct = lambda v: round(100 * v / total, 2) if total else 0.0            # noqa: E731
    loss = weighted * DAMAGE_RATIO
    return StressResult(
        horizon_years=horizon_years, scr_nonlife_meur=scr_nonlife, covered_sum_insured_meur=round(total, 2),
        excluded_no_cover_meur=round(sum(p["sum_insured_keur"] for p in properties if not p["flood_cover"]) / 1000, 2),
        unassessed_sum_insured_meur=round(sum(by_hazard.get("UNKNOWN", [])), 2),
        by_class=[ClassExposure(hazard=k, label=LABEL[k], zones=len(by_hazard[k]), sum_insured_meur=round(sum(by_hazard[k]), 2), share_pct=pct(sum(by_hazard[k])))
                  for k in ORDER if k in by_hazard],
        threatened_pct_of_scr=pct(weighted), threatened_scr_meur=round(scr_nonlife * weighted / total, 2) if total else 0.0,
        high_zone_pct=pct(sum(by_hazard.get("HIGH", []))), elevated_or_moderate_pct=pct(sum(by_hazard.get("HIGH", [])) + sum(by_hazard.get("MEDIUM", []))),
        scenario_loss_meur=round(loss, 2), scenario_loss_pct_of_scr=round(100 * loss / scr_nonlife, 2) if scr_nonlife else 0.0,
        damage_ratio=DAMAGE_RATIO, zones=rows,
        method=f"Probabilité, par zone, d'au moins un jour de pluie dommageable (>= 40 mm) sur l'horizon, déduite de 15 années d'un modèle climatique (relevée de 25 % si les rivières "
               f"sont déjà très hautes) ; SCR réparti au prorata de la valeur assurée ; taux de dommage supposé de {DAMAGE_RATIO:.0%} (non calibré sur l'historique de sinistres).")
