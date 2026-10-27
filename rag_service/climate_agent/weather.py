"""Open-Meteo access: the external, real-world data source of the Climate agent (no API key needed).

- climate-api.open-meteo.com : daily precipitation of a high-resolution climate model (EC_Earth3P_HR) over a 15-year reference window;
- flood-api.open-meteo.com   : river discharge (GloFAS) now and over the coming ~3 months.

From the 15 yearly maxima of daily rainfall we get the ANNUAL probability of a damaging rain day (>= EVENT_MM); over a horizon of N years
the probability of at least one such day is 1 - (1 - p)^N, so the horizon and the zone both move the result. A screening indicator, not a catastrophe model.
The climate series is a projection that does not change from one day to the next: it is cached on disk, which also spares the free API quota.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable

CLIMATE_URL = "https://climate-api.open-meteo.com/v1/climate"
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
CLIMATE_MODEL = "EC_Earth3P_HR"
REFERENCE_YEARS = 15                             # reference window: this year + 14 (a longer window is rejected by the free tier quota)
MIN_YEARS = 10
HEAVY_RAIN_MM = 20.0
EVENT_MM = 40.0                                  # a damaging rain day (ASSUMPTION, not a calibrated flood threshold)
HIGH_PROB, MEDIUM_PROB = 0.5, 0.2                # annual probability of such a day: base class
RIVER_ESCALATION, RIVER_FACTOR = 10.0, 1.25      # rivers already above 10x their mean: annual probability x 1.25 (ASSUMPTION)
CACHE_DIR = Path(os.getenv("CLIMATE_CACHE_DIR") or Path(__file__).with_name("cache"))
CACHE_TTL_S = 3600

Getter = Callable[[str, dict[str, Any]], dict[str, Any]]
_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}


def zone_key(lat: float, lon: float) -> str:
    """A zone is a 0.1-degree cell (~10 km): neighbouring addresses share one API call."""
    return f"{round(lat, 1):.1f},{round(lon, 1):.1f}"


def _get(url: str, params: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout) as response:   # noqa: S310 - fixed https hosts
        return json.loads(response.read().decode("utf-8"))


def effective_prob(annual_prob: float | None, river_ratio: float | None) -> float | None:
    if annual_prob is None:
        return None
    return min(1.0, annual_prob * RIVER_FACTOR) if (river_ratio or 0) >= RIVER_ESCALATION else annual_prob


def horizon_prob(annual_prob: float | None, horizon_years: int) -> float | None:
    return None if annual_prob is None else round(1 - (1 - annual_prob) ** horizon_years, 4)


def classify(annual_prob: float | None) -> str:
    if annual_prob is None:
        return "UNKNOWN"
    return "HIGH" if annual_prob >= HIGH_PROB else "MEDIUM" if annual_prob >= MEDIUM_PROB else "LOW"


def _climate_stats(daily: dict[str, Any]) -> dict[str, Any]:
    by_year: dict[str, float] = {}
    heavy = 0
    for t, p in zip(daily["time"], daily.get("precipitation_sum", [])):
        if p is not None:
            by_year[t[:4]] = max(by_year.get(t[:4], 0.0), p)
            heavy += p >= HEAVY_RAIN_MM
    if not by_year:
        raise ValueError("série de précipitations vide")
    years = list(by_year.values())
    return {"years": len(years), "rx1_mm": round(sum(years) / len(years), 1), "annual_event_prob": round(sum(y >= EVENT_MM for y in years) / len(years), 3),
            "heavy_rain_days_per_year": round(heavy / len(years), 1)}


def _river_ratio(daily: dict[str, Any]) -> float | None:
    peaks = [v for v in daily.get("river_discharge_max", []) if v is not None]
    means = [v for v in daily.get("river_discharge_mean", []) if v is not None]
    if not peaks or not means or sum(means) / len(means) <= 0.01:
        return None
    return round(max(peaks) / (sum(means) / len(means)), 2)


def _climate_series(lat: float, lon: float, zone: str, get: Getter, today: date, use_disk: bool) -> dict[str, Any]:
    path = CACHE_DIR / f"{zone.replace(',', '_')}_{CLIMATE_MODEL}_{today.year}.json"
    if use_disk and path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("years", 0) >= MIN_YEARS:            # a truncated / foreign file is not trusted
                return cached
        except (OSError, ValueError, AttributeError):
            pass
    daily = get(CLIMATE_URL, {"latitude": lat, "longitude": lon, "start_date": date(today.year, 1, 1).isoformat(), "end_date": date(today.year + REFERENCE_YEARS - 1, 12, 31).isoformat(),
                              "models": CLIMATE_MODEL, "daily": "precipitation_sum"})["daily"]
    stats = _climate_stats(daily)
    if use_disk and stats["years"] >= MIN_YEARS:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(stats), encoding="utf-8")
        except OSError:
            pass
    return stats


def fetch_hazard(lat: float, lon: float, horizon_years: int = 5, *, getter: Getter | None = None, today: date | None = None) -> dict[str, Any]:
    """Flood hazard of one zone. Never raises: a source that fails is reported in `errors`; without the climate series the zone is UNKNOWN."""
    get, today = getter or _get, today or date.today()
    zone = zone_key(lat, lon)
    cache_key = (zone, horizon_years, today.isoformat())
    cached = _CACHE.get(cache_key)
    if getter is None and cached and time.time() - cached[0] < CACHE_TTL_S:
        return cached[1]
    out: dict[str, Any] = {"zone": zone, "latitude": round(lat, 4), "longitude": round(lon, 4), "horizon_years": horizon_years, "rx1_mm": None,
                           "annual_event_prob": None, "horizon_event_prob": None, "heavy_rain_days_per_year": None,
                           "river_discharge_ratio": None, "errors": []}
    try:
        stats = _climate_series(lat, lon, zone, get, today, use_disk=getter is None)
        out.update({k: v for k, v in stats.items() if k != "years"})
    except Exception as exc:                      # noqa: BLE001 - network, quota, malformed answer
        out["errors"].append("API climat : quota gratuit Open-Meteo atteint, réessayez dans une heure (les zones déjà récupérées sont conservées)"
                             if "429" in str(exc) else f"API climat : {type(exc).__name__}: {str(exc)[:120]}")
    try:
        daily = get(FLOOD_URL, {"latitude": lat, "longitude": lon, "daily": "river_discharge,river_discharge_mean,river_discharge_max", "forecast_days": 92})["daily"]
        out["river_discharge_ratio"] = _river_ratio(daily)
    except Exception as exc:                      # noqa: BLE001
        out["errors"].append(f"API crues : {type(exc).__name__}: {str(exc)[:120]}")
    p = effective_prob(out["annual_event_prob"], out["river_discharge_ratio"])
    out["annual_event_prob"], out["horizon_event_prob"] = (round(p, 3) if p is not None else None), horizon_prob(p, horizon_years)
    out["flood_hazard"] = classify(p)
    out["source"] = f"Open-Meteo (modèle climatique {CLIMATE_MODEL} 15 ans + GloFAS)"
    if getter is None and out["flood_hazard"] != "UNKNOWN":
        _CACHE[cache_key] = (time.time(), out)
    return out
