"""Agent 2 - Risk Investigation Analyst (Gemini, strict structured output).

Turns the quantitative solvency results into an actuarial diagnosis for management / the supervisor.
Design rules:
  * the LLM never does arithmetic: shares, headroom to 100 % / 130 % and diversification are precomputed here;
  * the output is a typed Pydantic object (`with_structured_output`), never free text to parse;
  * a deterministic guardrail keeps the verdict coherent with the numbers (a 96 % ratio can never come back 'low').
"""
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError

from pipeline.schemas import AgentState, DataQualityReport, Finding, RiskDiagnosisReport, SolvencyMetrics

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# gemini-1.5-flash (the model first asked for) is retired by Google: the default is a current one, overridable.
ANALYST_MODEL = os.getenv("PIPELINE_LLM_MODEL", "gemini-3.5-flash")
# Tried only if the primary fails after its own retries (Google answers 503 "high demand" from time to time).
FALLBACK_MODEL = os.getenv("PIPELINE_FALLBACK_MODEL", "gemini-3-flash-preview")
ANALYST_MODELS = list(dict.fromkeys(m for m in (ANALYST_MODEL, FALLBACK_MODEL) if m))
REGULATORY_THRESHOLD = 100.0
APPETITE_THRESHOLD = 130.0
MIN_CHARS_FOR_DEGRADED_CHECK = 600     # a real diagnosis is several KB; very short text proves nothing
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
STATUS_OF = {"low": "normal", "medium": "watchlist", "high": "anomaly_detected"}

SYSTEM_PROMPT = """Tu es actuaire senior, responsable de l'audit des risques d'un organisme d'assurance soumis à la directive Solvabilité II (2009/138/CE, formule standard). Tu rédiges un diagnostic de niveau Direction générale / régulateur (ACPR).

RÈGLES DE RAISONNEMENT
1. Appuie-toi UNIQUEMENT sur les données fournies (résultats de solvabilité et indicateurs précalculés). N'invente aucun chiffre ; reprends les montants et pourcentages tels quels, sans refaire de calcul.
2. Seuils : ratio de couverture du SCR < 100 % = non-respect du SCR (plan de rétablissement exigé, art. 138) ; entre 100 % et 130 % = conforme mais sous le seuil d'appétit au risque interne de 130 % ; >= 130 % = confortable. Un MCR non couvert (art. 139) est plus grave encore.
3. Évalue la robustesse financière : marge ou déficit en M EUR par rapport à 100 % et 130 %, qualité des fonds propres (décote de classement, Tier 1) et poids de l'absorption par les impôts différés et les provisions techniques quand ils sont fournis.
4. Identifie les facteurs de risque prépondérants à partir des parts de modules fournies (marché dont actions, spread, taux ; défaut de contrepartie ; souscription Non-Vie / Vie / Santé) et explique pourquoi ils pèsent sur le SCR.
5. Recommandations ORSA concrètes (scénarios de stress à examiner, trajectoire de solvabilité, gouvernance). Plans d'atténuation adaptés aux modules dominants : cession en réassurance (quote-part, excédent de sinistres, stop-loss), titrisation / ILS, réallocation des actions vers des obligations souveraines, dé-risking, couverture de taux, renforcement des fonds propres (dette subordonnée Tier 2, augmentation de capital, réduction de dividende). Indique l'effet attendu sur le ratio sans le chiffrer au-delà des données.
6. Si la qualité des données est dégradée (statut WARNING), signale-le comme limite de l'analyse en citant les constats précis de `data_quality_findings` (jamais une recommandation générique sur la « qualité des données »).
7. Rédige en français correct, avec tous les accents et les apostrophes typographiques d'usage (l'organisme, d'après, qu'il, à, é, è), en phrases simples, sans aucune mise en forme Markdown (pas d'astérisques, de dièses, de puces ni de tirets de liste).
8. Renseigne chaque champ du schéma demandé ; `findings` contient 3 à 6 constats chiffrés, `evidence` cite les chiffres exacts des données.
9. Si `details.source` vaut "reported", les chiffres viennent d'un document de synthèse publié et n'ont pas été recalculés par la formule standard : mentionne-le comme limite de vérification, mais ce n'est PAS un défaut de qualité des données ; juge la solvabilité sur les chiffres publiés et les contrôles de cohérence fournis."""


# ----------------------------------------------------------------------------- deterministic helpers


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def compute_indicators(solvency: SolvencyMetrics, quality: DataQualityReport | None) -> dict[str, Any]:
    """Everything the LLM could be tempted to compute, computed here."""
    ratio = float(solvency.ratio or 0.0)
    details = solvency.details
    modules_sum = sum(m.value for m in solvency.modules)
    shares = sorted(
        ({"module": m.name, "value_meur": round(m.value, 2), "share_of_modules_pct": round(m.value / modules_sum * 100, 1)}
         for m in solvency.modules if modules_sum > 0),
        key=lambda s: s["share_of_modules_pct"], reverse=True)
    mcr_ratio = _num(details.get("mcrRatio"))
    if mcr_ratio is None and solvency.mcr > 0 and _num(details.get("eligibleFundsMcr")) is not None:
        mcr_ratio = round(float(details["eligibleFundsMcr"]) / solvency.mcr * 100, 2)
    diversification = _num(details.get("diversificationBenefit"))
    out: dict[str, Any] = {
        "solvency_ratio_pct": round(ratio, 2),
        "surplus_over_100pct_meur": round(solvency.eligible_funds - solvency.scr, 2),
        "surplus_over_130pct_meur": round(solvency.eligible_funds - APPETITE_THRESHOLD / 100 * solvency.scr, 2),
        "regulatory_position": "BREACH" if ratio < REGULATORY_THRESHOLD else "BELOW_APPETITE" if ratio < APPETITE_THRESHOLD else "COMFORTABLE",
        "mcr_ratio_pct": mcr_ratio,
        "module_shares": shares,
        "sum_of_modules_meur": round(modules_sum, 2),
        "diversification_benefit_pct_of_modules": round(diversification / modules_sum * 100, 1) if diversification is not None and modules_sum > 0 else None,
        "data_quality_status": quality.status if quality else None,
        "data_quality_score": quality.quality_score if quality else None,
        # what the audit actually found: without it the model can only say "improve data quality" in general
        "data_quality_findings": [f"{a.severity.value} {a.code} : {a.message}" for a in quality.anomalies[:6]] if quality else [],
    }
    return out


def expected_severity(indicators: dict[str, Any]) -> str:
    """The least severe verdict the numbers allow."""
    mcr_ratio = indicators.get("mcr_ratio_pct")
    if indicators["solvency_ratio_pct"] < REGULATORY_THRESHOLD or (mcr_ratio is not None and mcr_ratio < 100):
        return "high"
    if indicators["solvency_ratio_pct"] < APPETITE_THRESHOLD:
        return "medium"
    return "low"


_ELISION = re.compile(r"\b(qu|Qu|[ldjnscLDJNSC])\s+(?=[aeiouyàâäéèêëîïôöùûüh])")   # lowercase vowel only: 'M EUR' / '10 M euros' are never touched


def _map_strings(value: Any, fn) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    return value


def is_degraded_text(report: RiskDiagnosisReport) -> bool:
    """Gemini occasionally returns a whole report without a single apostrophe ('L organisme', 'd appétit') and sometimes
    with accents stripped too ('retablissement'). A genuine French report of this length always contains apostrophes."""
    blob = json.dumps(report.model_dump(), ensure_ascii=False)
    return len(blob) >= MIN_CHARS_FOR_DEGRADED_CHECK and "'" not in blob and "’" not in blob


def restore_elisions(report: RiskDiagnosisReport) -> tuple[RiskDiagnosisReport, bool]:
    """Last resort once retries are exhausted: restores French elisions, only on a degraded report (see is_degraded_text).
    Accents cannot be restored deterministically; they are only improved by retrying."""
    if not is_degraded_text(report):
        return report, False
    data = report.model_dump()
    fixed = _map_strings(data, lambda s: _ELISION.sub(lambda m: m.group(1) + "'", s))
    if fixed == data:
        return report, False
    return RiskDiagnosisReport.model_validate(fixed), True


def apply_guardrails(report: RiskDiagnosisReport, indicators: dict[str, Any]) -> tuple[RiskDiagnosisReport, list[str]]:
    """Severity may be raised by one level above what the numbers imply (e.g. degraded data), never lowered below it
    and never inflated by more. `status` is then derived from the final severity so the two can never disagree."""
    notes: list[str] = []
    floor = expected_severity(indicators)
    ceiling = {"low": "medium", "medium": "high", "high": "high"}[floor]
    final = report.severity
    if SEVERITY_RANK[final] < SEVERITY_RANK[floor]:
        final = floor
        notes.append(f"Gravité relevée de '{report.severity}' à '{floor}' : incompatible avec un ratio de {indicators['solvency_ratio_pct']} %.")
    elif SEVERITY_RANK[final] > SEVERITY_RANK[ceiling]:
        final = ceiling
        notes.append(f"Gravité ramenée de '{report.severity}' à '{ceiling}' : non justifiée par un ratio de {indicators['solvency_ratio_pct']} %.")
    status = STATUS_OF[final]
    if final != report.severity or status != report.status:
        report = report.model_copy(update={"severity": final, "status": status})
    return report, notes


# ----------------------------------------------------------------------------- LLM


KEY_ENV_VARS = ("INVESTIGATOR_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")   # the Investigator has its own key; the shared one is the fallback


def investigator_key_source() -> str | None:
    """Name of the environment variable the analyst will use (never the key itself)."""
    return next((name for name in KEY_ENV_VARS if os.getenv(name)), None)


@lru_cache(maxsize=4)
def _structured_llm(model: str):
    """One client per model, built lazily so importing the package (tests, CORS preflight) never needs the API key."""
    source = investigator_key_source()
    if source is None:
        raise RuntimeError("INVESTIGATOR_API_KEY (or GEMINI_API_KEY) is not set in rag_service/.env.")
    api_key = os.environ[source]
    llm = ChatGoogleGenerativeAI(model=model, google_api_key=api_key, temperature=0.1, max_retries=3, timeout=60)
    return llm.with_structured_output(RiskDiagnosisReport)


def _user_message(solvency: SolvencyMetrics, indicators: dict[str, Any]) -> str:
    facts = {
        "resultats_solvabilite": {
            "scr_meur": solvency.scr, "bscr_meur": solvency.bscr, "mcr_meur": solvency.mcr,
            "fonds_propres_eligibles_meur": solvency.eligible_funds, "ratio_solvabilite_pct": solvency.ratio,
            "modules": [m.model_dump() for m in solvency.modules], "details": solvency.details,
        },
        "indicateurs_precalcules": indicators,
    }
    return "Produis le diagnostic actuariel à partir de ces données :\n" + json.dumps(facts, ensure_ascii=False, indent=2, default=str)


def risk_analyst_node(state: AgentState) -> dict:
    """LangGraph node: writes state['diagnosis'] (typed) or, if the LLM cannot answer, an error and ANALYSIS_FAILED."""
    solvency = state["solvency"]
    quality = state.get("data_quality")
    indicators = compute_indicators(solvency, quality)
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=_user_message(solvency, indicators))]

    errors: list[str] = []
    for model in ANALYST_MODELS:
        for _attempt in range(2):   # transport errors are retried inside the client (max_retries=3); this covers schema failures
            try:
                raw = _structured_llm(model).invoke(messages)
                if raw is None:
                    raise ValueError("le modèle n'a renvoyé aucun objet structuré")
                report = raw if isinstance(raw, RiskDiagnosisReport) else RiskDiagnosisReport.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                errors_msg = f"{model} : sortie structurée invalide : {str(exc)[:200]}"
                if _attempt == 1:
                    errors.append(errors_msg)
                continue
            except Exception as exc:    # network, quota, 5xx after retries, missing key...
                errors.append(f"{model} indisponible ({type(exc).__name__}) : {str(exc)[:200]}")
                break                   # no point asking the same model again: next model
            if is_degraded_text(report) and _attempt == 0:
                continue            # degraded French (no apostrophes / accents): ask again once before settling for a repair
            report, repaired = restore_elisions(report)
            report, notes = apply_guardrails(report, indicators)
            if repaired:
                notes.append("Texte dégradé après nouvel essai : apostrophes restaurées automatiquement (accents non garantis).")
            if model != ANALYST_MODELS[0]:
                notes.append(f"Modèle de secours utilisé ({model}) : {errors[-1] if errors else 'modèle principal indisponible'}")
            return {"diagnosis": report, "diagnosis_source": "llm", "model_used": model, "guardrail_notes": notes, "pipeline_status": "COMPLETED"}
    return {"errors": errors or ["Analyste : aucune réponse"], "pipeline_status": "ANALYSIS_FAILED"}


# ----------------------------------------------------------------------------- blocked branch (no LLM)


def blocked_alert_node(state: AgentState) -> dict:
    """Used when the data quality gate says BLOCKED: a deterministic alert in the same shape as a diagnosis, no LLM call."""
    quality = state["data_quality"]
    critical = [a for a in quality.anomalies if a.severity.value == "CRITICAL"]
    n_critical = quality.counts_by_severity.get("CRITICAL", len(critical))
    by_code: dict[str, list] = {}
    for anomaly in critical:
        by_code.setdefault(anomaly.code, []).append(anomaly)
    findings = []
    for code, items in sorted(by_code.items(), key=lambda kv: -len(kv[1]))[:6]:
        rows = ", ".join(str(a.row) for a in items[:5] if a.row is not None)
        findings.append(Finding(
            indicator=code,
            evidence=f"{quality.counts_by_code.get(code, len(items))} occurrence(s)" + (f", lignes {rows}" if rows else ""),
            interpretation=items[0].message))
    if not findings:
        findings.append(Finding(indicator="QUALITE_DONNEES", evidence=f"score {quality.quality_score}/100",
                                interpretation="Des anomalies critiques empêchent le calcul du SCR."))
    report = RiskDiagnosisReport(
        status="anomaly_detected", severity="high",
        summary=(f"Le calcul du SCR est suspendu : l'audit qualité détecte {n_critical} anomalie(s) critique(s) sur {quality.rows_audited} position(s) "
                 f"(score de qualité {quality.quality_score}/100). Aucun diagnostic de solvabilité n'est émis tant que les données ne sont pas corrigées."),
        robustness_assessment="Non évaluable : les données d'entrée ne satisfont pas aux exigences de qualité de l'article 82 de la directive.",
        findings=findings, risk_drivers=[],
        orsa_recommendations=["Consigner la remédiation dans le registre de qualité des données et la gouvernance associée (fonction actuarielle).",
                              "Relancer l'audit qualité puis le calcul du SCR après correction des anomalies critiques."],
        mitigation_plans=[],
        recommended_action="Corriger les anomalies critiques listées dans le rapport qualité, puis relancer l'audit avant tout calcul du SCR.")
    return {"diagnosis": report, "diagnosis_source": "alert", "pipeline_status": "BLOCKED"}
