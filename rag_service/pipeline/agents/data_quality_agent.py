"""Agent 1 - Data Quality & Compliance (Pillar 1, upstream of the SCR calculation).

Deterministic business rules, no LLM. Audits the raw portfolio against the four data-quality criteria of
Art. 82 of Directive 2009/138/EC:

  completeness  - mandatory fields present (id, exposure, rating, duration, premiums / claims)
  accuracy      - numbers parse, are in bounds, ratings belong to a recognised ECAI scale
  relevance     - dates are coherent (maturity vs effective vs valuation date)
  traceability  - the extract carries source metadata

Severity policy: CRITICAL = no reliable SCR can be computed from this row/file (blocks the pipeline),
MEDIUM = the SCR is computable but a module may be mis-stated, LOW = hygiene.
"""
import math
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from pipeline.schemas import AgentState, Anomaly, Criterion, DataQualityReport, PortfolioPayload, Severity

MAX_LISTED_ANOMALIES = 200
MAX_DURATION_YEARS = 100.0

# canonical field -> accepted column names (FR/EN), compared after normalisation (lower-case, no accents, '_' separators)
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "position_id", "identifiant", "isin", "code", "contract_id", "policy_id", "numero_police"),
    "exposure": ("exposure", "exposition", "market_value", "valeur_marche", "montant", "notional", "nominal", "amount"),
    "rating": ("rating", "notation", "credit_rating", "note", "ecai_rating"),
    "duration": ("duration", "duree", "modified_duration", "duration_modifiee"),
    "premium": ("premium", "premiums", "prime", "primes", "gross_premium", "primes_acquises"),
    "claims": ("claims", "claim", "sinistre", "sinistres", "claims_amount", "sinistres_payes", "charge_sinistres"),
    "effective_date": ("effective_date", "date_effet", "start_date", "date_debut", "inception_date"),
    "maturity_date": ("maturity_date", "date_echeance", "echeance", "end_date", "date_fin", "expiry_date"),
    "asset_class": ("asset_class", "classe_actif", "type", "record_type", "categorie", "category", "line_of_business"),
}

FIXED_INCOME_CLASSES = {"bond", "bonds", "obligation", "obligations", "fixed_income", "credit", "loan", "pret", "corporate",
                        "sovereign", "souverain", "government", "covered_bond", "structured", "abs", "mbs", "taux"}
INSURANCE_CLASSES = {"insurance", "assurance", "non_life", "non_vie", "life", "vie", "health", "sante", "policy", "contrat",
                     "liability", "passif", "iard", "property_casualty"}

# Recognised ECAI scales: S&P / Fitch, Moody's (compared upper-case), plus Solvency II credit quality steps and "not rated".
SP_FITCH = {"AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
            "CCC+", "CCC", "CCC-", "CC", "C", "RD", "SD", "D"}
MOODYS = {"AAA", "AA1", "AA2", "AA3", "A1", "A2", "A3", "BAA1", "BAA2", "BAA3", "BA1", "BA2", "BA3", "B1", "B2", "B3",
          "CAA1", "CAA2", "CAA3", "CA", "C"}
CQS = re.compile(r"^CQS ?[0-6]$")
UNRATED = {"NR", "N/R", "UNRATED", "NOT RATED", "NON NOTE", "NON NOTÉ", "SANS NOTATION"}

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d")
ROW_WEIGHT = {Severity.CRITICAL: 1.0, Severity.MEDIUM: 0.4, Severity.LOW: 0.1}      # share of a row's quality lost
GLOBAL_POINTS = {Severity.CRITICAL: 20.0, Severity.MEDIUM: 5.0, Severity.LOW: 1.0}   # points lost for portfolio-level findings
SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


# ----------------------------------------------------------------------------- parsing helpers


def _norm_key(key: Any) -> str:
    text = unicodedata.normalize("NFD", str(key).strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def _to_float(value: Any) -> float | None:
    """Parses 1234.5, '1 234,5', '(12)', '12 %'. None when it is not a finite number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip().replace(" ", "").replace(" ", "").replace("€", "").replace("%", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -number if negative else number


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 20000 <= value <= 80000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()      # Excel serial date
    if isinstance(value, str):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _canonical(record: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """canonical field -> (original column name, raw value), first alias found wins."""
    by_norm = {_norm_key(k): (k, v) for k, v in record.items()}
    out: dict[str, tuple[str, Any]] = {}
    for canon, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                out[canon] = by_norm[alias]
                break
    return out


def _rating_state(raw: Any) -> str:
    """'valid' | 'unrated' | 'invalid'"""
    text = re.sub(r"\s+", " ", str(raw).strip().upper())
    if text in UNRATED:
        return "unrated"
    if text in SP_FITCH or text in MOODYS or CQS.match(text):
        return "valid"
    return "invalid"


# ----------------------------------------------------------------------------- QRT mode
# The app holds aggregated QRT tables (code / label / amounts), not position-level data. Those are audited with rules
# that fit them: a position rule such as "exposure < 0 is critical" would flag legitimate lines (loss absorption...).

QRT_LABEL_KEYS = ("libelle", "rubrique", "label", "nom", "name", "description")
QRT_LABEL_PREFIXES = ("libelle", "rubrique", "label", "intitule", "description", "element")
QRT_CODE_KEYS = ("code", "code_eiopa", "code_noeud", "ref", "ref_eiopa")
QRT_VALUE_PREFIXES = ("net", "brut", "valeur", "value", "montant")
STRONG_EXPOSURE_KEYS = ("exposure", "exposition", "market_value", "valeur_marche", "notional", "nominal")
QRT_CODE = re.compile(r"S\.?\s?\d{2}\.?\s?\d{2}|\bR\d{4}\b|\bC\d{4}\b", re.IGNORECASE)   # S.25.01, R0010, C0030
QRT_UNITS = {"", "m€", "k€", "md€", "mrd€", "€", "eur", "meur", "keur", "mdeur", "%", "ratio"}
# lines that are legitimately negative: adjustments, diversification, loss absorption, deductions
QRT_NEGATIVE_OK = re.compile(r"diversif|absorption|ajustement|adjustment|loss_absorb|(^|_)lac(_|$)|ecart|variation|resultat|reconciliation|deduction|elimination")


def _qrt_view(record: dict[str, Any]):
    by_norm = {_norm_key(k): (k, v) for k, v in record.items()}

    def first(keys: tuple[str, ...]):
        return next((by_norm[k] for k in keys if k in by_norm), None)

    def first_prefix(prefixes: tuple[str, ...]):       # 'Rubrique / Élément Solvabilité II' -> rubrique_element_solvabilite_ii
        return next((v for nk, v in by_norm.items() if nk.startswith(prefixes)), None)

    values = [(k, v) for nk, (k, v) in by_norm.items() if nk.startswith(QRT_VALUE_PREFIXES)]
    unit = first(("unite", "unit", "currency", "devise"))
    return first(QRT_CODE_KEYS) or first_prefix(("code",)), first(QRT_LABEL_KEYS) or first_prefix(QRT_LABEL_PREFIXES), values, unit, record.get("_sheet")


def _is_qrt(positions: list[dict[str, Any]]) -> bool:
    """QRT mode when most rows look like 'code / label / amount' and none carries an exposure column."""
    if not positions:
        return False
    hits = 0
    for record in positions:
        _code, label, values, _unit, _sheet = _qrt_view(record)
        has_exposure = any(_norm_key(k) in STRONG_EXPOSURE_KEYS for k in record)
        hits += bool(label and values and not has_exposure)
    return hits / len(positions) >= 0.6


def _audit_qrt_rows(positions: list[dict[str, Any]], found: "_Collector") -> None:
    seen: dict[tuple, tuple] = {}
    coded = 0
    for index, record in enumerate(positions, start=1):
        code, label, values, unit, sheet = _qrt_view(record)
        code_txt = None if code is None or _is_missing(code[1]) else str(code[1]).strip()
        label_txt = None if label is None or _is_missing(label[1]) else str(label[1]).strip()
        rid = code_txt or label_txt
        where = f"{sheet}." if sheet else ""
        coded += bool(code_txt and QRT_CODE.search(code_txt))

        def add(anomaly_code: str, crit: Criterion, sev: Severity, msg: str, field: str, value: Any = None) -> None:
            found.add(anomaly_code, crit, sev, msg, field=f"{where}{field}", row=index, record_id=rid, value=value)

        if code_txt is None:
            add("QRT_CODE_MISSING", Criterion.COMPLETENESS, Severity.LOW, "Code de ligne QRT manquant.", code[0] if code else "code")
        if label_txt is None:
            add("QRT_LABEL_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, "Libellé de ligne manquant.", label[0] if label else "libelle")

        filled = [(k, v) for k, v in values if not _is_missing(v)]
        if not filled:
            add("QRT_VALUE_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, "Aucun montant renseigné sur la ligne.", values[0][0] if values else "valeur")
        negative_ok = bool(QRT_NEGATIVE_OK.search(_norm_key(label_txt or "")))
        for key, raw_value in filled:
            number = _to_float(raw_value)
            if number is None:
                add("QRT_VALUE_NOT_NUMERIC", Criterion.ACCURACY, Severity.CRITICAL, "Montant non numérique.", key, raw_value)
            elif number < 0 and not negative_ok:
                add("QRT_VALUE_NEGATIVE", Criterion.ACCURACY, Severity.CRITICAL,
                    "Montant négatif sur une ligne qui ne peut pas l'être (seuls les ajustements, la diversification et l'absorption des pertes le peuvent).", key, number)

        if unit is not None and not _is_missing(unit[1]) and str(unit[1]).lower().replace(" ", "") not in QRT_UNITS:
            add("QRT_UNIT_UNKNOWN", Criterion.ACCURACY, Severity.LOW, "Unité non reconnue.", unit[0], unit[1])

        if rid is not None:
            key = (sheet, code_txt, label_txt)
            signature = tuple(_to_float(v) for _k, v in filled)
            if key in seen and seen[key] != signature:
                add("QRT_DUPLICATE_LINE", Criterion.ACCURACY, Severity.MEDIUM, f"Ligne « {rid} » présente plusieurs fois avec des montants différents.", "code")
            seen.setdefault(key, signature)
    if positions and not coded:
        # The SCR engine matches lines by QRT code first and falls back on label keywords: without a single code it would
        # take an asset value ("Actions cotées 3 692") for an equity risk charge. Not a QRT: stop before the calculation.
        found.add("QRT_NO_TEMPLATE_CODES", Criterion.RELEVANCE, Severity.CRITICAL,
                  "Aucune ligne ne porte de code QRT (S.23.01, S.25.01, S.28.01...) : ce document n'est pas un état QRT, "
                  "la formule standard ne peut pas être recalculée à partir de ses libellés.", field="code")


# ----------------------------------------------------------------------------- summary mode
# A prudential report publishes results (SCR, own funds, MCR, ratios) without the standard-formula inputs. They are not
# recomputed; what can be audited is whether they are present and consistent with each other.

RATIO_TOLERANCE_PTS = 1.0
MCR_CORRIDOR = (25.0, 45.0)                 # MCR between 25 % and 45 % of the SCR (Directive art. 129), unless the AMCR floor binds


def _audit_summary(figures: dict[str, Any], found: "_Collector") -> None:
    def add(code: str, crit: Criterion, sev: Severity, msg: str, field: str, value: Any = None) -> None:
        found.add(code, crit, sev, msg, field=field, row=None, record_id="synthèse", value=value)   # document-level: one record, scored in points not per row

    num = lambda key: _to_float(figures.get(key))   # noqa: E731
    scr, funds, mcr = num("scr"), num("eligible_funds"), num("mcr")
    ratio, mcr_ratio = num("ratio_scr_pct"), num("ratio_mcr_pct")

    if scr is None or scr <= 0:
        add("SUMMARY_SCR_MISSING", Criterion.COMPLETENESS, Severity.CRITICAL, "SCR publié absent ou nul : aucun ratio ne peut être établi.", "scr", figures.get("scr"))
    if funds is None:
        add("SUMMARY_FUNDS_MISSING", Criterion.COMPLETENESS, Severity.CRITICAL, "Fonds propres éligibles absents du document.", "eligible_funds")
    elif funds < 0:
        add("SUMMARY_FUNDS_NEGATIVE", Criterion.ACCURACY, Severity.CRITICAL, "Fonds propres éligibles négatifs.", "eligible_funds", funds)
    if mcr is None:
        add("SUMMARY_MCR_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, "MCR absent du document : la couverture du MCR ne peut pas être vérifiée.", "mcr")

    if scr and scr > 0 and funds is not None:
        computed = funds / scr * 100
        if ratio is not None and abs(ratio - computed) > RATIO_TOLERANCE_PTS:
            add("SUMMARY_RATIO_MISMATCH", Criterion.ACCURACY, Severity.MEDIUM,
                f"Ratio SCR publié ({ratio:.1f} %) différent de fonds propres / SCR ({computed:.1f} %).", "ratio_scr_pct", ratio)
    if mcr and mcr > 0 and funds is not None and mcr_ratio is not None and mcr_ratio > funds / mcr * 100 + RATIO_TOLERANCE_PTS:
        # own funds eligible for the MCR can only be lower than those for the SCR (tier limits), never higher
        add("SUMMARY_MCR_RATIO_MISMATCH", Criterion.ACCURACY, Severity.MEDIUM,
            f"Ratio MCR publié ({mcr_ratio:.1f} %) supérieur au maximum possible ({funds / mcr * 100:.1f} %).", "ratio_mcr_pct", mcr_ratio)
    if mcr and scr and scr > 0:
        share = mcr / scr * 100
        if not (MCR_CORRIDOR[0] - 0.5 <= share <= MCR_CORRIDOR[1] + 0.5):
            add("SUMMARY_MCR_CORRIDOR", Criterion.RELEVANCE, Severity.MEDIUM,
                f"MCR = {share:.1f} % du SCR, hors du corridor 25-45 % : possible seulement si le plancher absolu (AMCR) s'applique, à vérifier.", "mcr", mcr)

    add("SUMMARY_NOT_RECOMPUTED", Criterion.TRACEABILITY, Severity.LOW,
        "Chiffres publiés par le document, repris tels quels : la formule standard n'a pas été recalculée.", "document_kind", "summary")


# ----------------------------------------------------------------------------- the audit


class _Collector:
    def __init__(self) -> None:
        self.items: list[Anomaly] = []

    def add(self, code: str, criterion: Criterion, severity: Severity, message: str, *, field: str | None = None,
            row: int | None = None, record_id: str | None = None, value: Any = None) -> None:
        shown = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
        self.items.append(Anomaly(code=code, criterion=criterion, severity=severity, message=message, field=field,
                                  row=row, record_id=record_id, value=shown))


def audit_portfolio(portfolio: PortfolioPayload) -> DataQualityReport:
    """Pure function (no I/O): audits the portfolio and returns the full quality report."""
    found = _Collector()
    positions = portfolio.positions
    n_rows = len(positions)
    summary = portfolio.document_kind == "summary"   # published results of a prudential report, not calculation inputs
    qrt = not summary and _is_qrt(positions)          # aggregated QRT tables rather than position-level data

    # ---- portfolio level -----------------------------------------------------------------------------------------
    if n_rows == 0:
        found.add("EMPTY_PORTFOLIO", Criterion.COMPLETENESS, Severity.CRITICAL, "Le portefeuille ne contient aucune position.")

    valuation = None
    if _is_missing(portfolio.valuation_date):
        found.add("VALUATION_DATE_MISSING", Criterion.RELEVANCE, Severity.LOW if qrt or summary else Severity.MEDIUM,
                  "Date de valorisation absente : la cohérence temporelle des échéances ne peut pas être contrôlée.", field="valuation_date")
    else:
        valuation = _to_date(portfolio.valuation_date)
        if valuation is None:
            found.add("VALUATION_DATE_INVALID", Criterion.ACCURACY, Severity.MEDIUM, "Date de valorisation illisible.",
                      field="valuation_date", value=portfolio.valuation_date)

    source = portfolio.source
    if source is None:
        found.add("SOURCE_MISSING", Criterion.TRACEABILITY, Severity.MEDIUM,
                  "Aucune métadonnée de source (système, date d'extraction, référence) : traçabilité non démontrable.", field="source")
    else:
        for attr, label in (("system", "système source"), ("extraction_date", "date d'extraction"), ("reference", "référence du lot / fichier")):
            if _is_missing(getattr(source, attr)):
                found.add(f"SOURCE_{attr.upper()}_MISSING", Criterion.TRACEABILITY, Severity.LOW,
                          f"Métadonnée de source manquante : {label}.", field=f"source.{attr}")

    # ---- row level -----------------------------------------------------------------------------------------------
    if summary:
        _audit_summary(positions[0] if positions else {}, found)
        return _build_report(found.items, max(1, n_rows))
    if qrt:
        _audit_qrt_rows(positions, found)
        return _build_report(found.items, n_rows)

    # columns present anywhere in the portfolio: a row of unknown class is held to the schema its peers use
    portfolio_fields: set[str] = set()
    for record in positions:
        portfolio_fields.update(_canonical(record))

    ids_seen: Counter[str] = Counter()
    for index, record in enumerate(positions, start=1):
        fields = _canonical(record)

        def raw(name: str) -> Any:
            return fields[name][1] if name in fields else None

        def col(name: str, default: str) -> str:
            return fields[name][0] if name in fields else default

        rid = None if _is_missing(raw("id")) else str(raw("id")).strip()

        def add(code: str, crit: Criterion, sev: Severity, msg: str, name: str, value: Any = None) -> None:
            found.add(code, crit, sev, msg, field=col(name, name), row=index, record_id=rid, value=value)

        # record nature: drives which fields are mandatory
        klass = _norm_key(raw("asset_class")) if not _is_missing(raw("asset_class")) else ""
        is_insurance = klass in INSURANCE_CLASSES or (not klass and ("premium" in fields or "claims" in fields))
        is_fixed_income = klass in FIXED_INCOME_CLASSES or (
            not klass and not is_insurance and bool(portfolio_fields & {"rating", "duration", "maturity_date"}))

        # completeness: identifier + exposure are always mandatory
        if rid is None:
            add("ID_MISSING", Criterion.COMPLETENESS, Severity.CRITICAL, "Identifiant de position manquant.", "id")
        else:
            ids_seen[rid] += 1
            if ids_seen[rid] == 2:
                add("ID_DUPLICATE", Criterion.ACCURACY, Severity.MEDIUM, f"Identifiant dupliqué « {rid} » : risque de double comptage.", "id", rid)

        if not is_insurance or "exposure" in fields:
            if _is_missing(raw("exposure")):
                add("EXPOSURE_MISSING", Criterion.COMPLETENESS, Severity.CRITICAL, "Exposition manquante.", "exposure")
            else:
                exposure = _to_float(raw("exposure"))
                if exposure is None:
                    add("EXPOSURE_NOT_NUMERIC", Criterion.ACCURACY, Severity.CRITICAL, "Exposition non numérique.", "exposure", raw("exposure"))
                elif exposure < 0:
                    add("EXPOSURE_NEGATIVE", Criterion.ACCURACY, Severity.CRITICAL, "Exposition négative.", "exposure", exposure)
                elif exposure == 0:
                    add("EXPOSURE_ZERO", Criterion.ACCURACY, Severity.LOW, "Exposition nulle : position sans effet sur le SCR.", "exposure", exposure)

        if is_fixed_income:
            if _is_missing(raw("rating")):
                add("RATING_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, "Notation de crédit manquante (nécessaire au risque de spread / défaut).", "rating")
            else:
                state = _rating_state(raw("rating"))
                if state == "invalid":
                    add("RATING_INVALID", Criterion.ACCURACY, Severity.MEDIUM,
                        "Notation hors des échelles reconnues (S&P / Fitch, Moody's, échelons CQS).", "rating", raw("rating"))
                elif state == "unrated":
                    add("RATING_UNRATED", Criterion.ACCURACY, Severity.LOW, "Position non notée : traitement prudentiel spécifique.", "rating", raw("rating"))

            if _is_missing(raw("duration")):
                add("DURATION_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, "Duration manquante (nécessaire au risque de taux).", "duration")
            else:
                duration = _to_float(raw("duration"))
                if duration is None:
                    add("DURATION_NOT_NUMERIC", Criterion.ACCURACY, Severity.MEDIUM, "Duration non numérique.", "duration", raw("duration"))
                elif duration < 0:
                    add("DURATION_NEGATIVE", Criterion.ACCURACY, Severity.CRITICAL, "Duration négative.", "duration", duration)
                elif duration > MAX_DURATION_YEARS:
                    add("DURATION_IMPLAUSIBLE", Criterion.ACCURACY, Severity.MEDIUM,
                        f"Duration supérieure à {MAX_DURATION_YEARS:.0f} ans : probable erreur d'unité.", "duration", duration)

        if is_insurance:
            for name, label in (("premium", "primes"), ("claims", "sinistres")):
                if _is_missing(raw(name)):
                    add(f"{name.upper()}_MISSING", Criterion.COMPLETENESS, Severity.MEDIUM, f"Montant de {label} manquant.", name)
                    continue
                amount = _to_float(raw(name))
                if amount is None:
                    add(f"{name.upper()}_NOT_NUMERIC", Criterion.ACCURACY, Severity.MEDIUM, f"Montant de {label} non numérique.", name, raw(name))
                elif amount < 0:
                    add(f"{name.upper()}_NEGATIVE", Criterion.ACCURACY, Severity.MEDIUM, f"Montant de {label} négatif (annulation ou erreur de signe ?).", name, amount)

        # relevance: temporal coherence
        effective = maturity = None
        for name, label in (("effective_date", "d'effet"), ("maturity_date", "d'échéance")):
            if _is_missing(raw(name)):
                continue
            parsed = _to_date(raw(name))
            if parsed is None:
                add(f"{name.upper()}_INVALID", Criterion.ACCURACY, Severity.MEDIUM, f"Date {label} illisible.", name, raw(name))
            elif name == "effective_date":
                effective = parsed
            else:
                maturity = parsed
        if effective and maturity and maturity < effective:
            add("MATURITY_BEFORE_EFFECTIVE", Criterion.RELEVANCE, Severity.CRITICAL,
                f"Échéance ({maturity.isoformat()}) antérieure à la date d'effet ({effective.isoformat()}).", "maturity_date", maturity.isoformat())
        if valuation and maturity and maturity < valuation:
            add("MATURITY_BEFORE_VALUATION", Criterion.RELEVANCE, Severity.MEDIUM,
                f"Position échue ({maturity.isoformat()}) avant la date de valorisation ({valuation.isoformat()}) : à exclure du périmètre.",
                "maturity_date", maturity.isoformat())
        if valuation and effective and effective > valuation:
            add("EFFECTIVE_AFTER_VALUATION", Criterion.RELEVANCE, Severity.LOW,
                f"Date d'effet ({effective.isoformat()}) postérieure à la date de valorisation.", "effective_date", effective.isoformat())

    return _build_report(found.items, n_rows)


def _build_report(anomalies: list[Anomaly], n_rows: int) -> DataQualityReport:
    # score = 100 x (1 - mean row penalty) - points for portfolio-level findings
    row_penalty: dict[int, float] = {}
    global_points = 0.0
    for a in anomalies:
        if a.row is None:
            global_points += GLOBAL_POINTS[a.severity]
        else:
            row_penalty[a.row] = row_penalty.get(a.row, 0.0) + ROW_WEIGHT[a.severity]
    mean_penalty = sum(min(1.0, p) for p in row_penalty.values()) / n_rows if n_rows else 0.0
    score = max(0.0, min(100.0, 100.0 * (1.0 - mean_penalty) - global_points))

    severities = {a.severity for a in anomalies}
    if Severity.CRITICAL in severities:
        status = "BLOCKED"
    elif Severity.MEDIUM in severities or score < 95.0:
        status = "WARNING"
    else:
        status = "PASSED"

    ordered = sorted(anomalies, key=lambda a: (SEVERITY_ORDER[a.severity], a.row or 0))
    return DataQualityReport(
        quality_score=round(score, 1),
        status=status,
        is_eligible_for_scr=Severity.CRITICAL not in severities,
        rows_audited=n_rows,
        anomalies=ordered[:MAX_LISTED_ANOMALIES],
        anomalies_truncated=max(0, len(ordered) - MAX_LISTED_ANOMALIES),
        counts_by_severity={s.value: sum(1 for a in anomalies if a.severity == s) for s in Severity},
        counts_by_code=dict(Counter(a.code for a in anomalies).most_common()),
    )


# ----------------------------------------------------------------------------- LangGraph node


def data_quality_node(state: AgentState) -> dict:
    """LangGraph node: audits state['portfolio'] and writes state['data_quality']."""
    return {"data_quality": audit_portfolio(state["portfolio"])}
