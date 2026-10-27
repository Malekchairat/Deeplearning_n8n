"""PDF -> QRT sheets: the PDF counterpart of the Excel import.

The output has exactly the shape of the Excel parser (`{ sheet name: [ {original header: value}, ... ] }`), so the audit,
the SCR engine, the analyst and the report downstream do not know (or care) where the data came from.

Two strategies, cheapest first:
  1. tables  - PyMuPDF reads the ruled tables of a text PDF. Deterministic, free, exact numbers.
  2. vision  - scanned PDF, or a layout without detectable tables: Gemini reads the document (multimodal) and returns
               typed lines. An LLM can misread a figure, so the result is flagged and still goes through the data
               quality audit before any calculation.
"""
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Literal

import pymupdf
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MAX_PAGES = 40
MIN_TABLE_ROWS = 3                     # fewer lines than this from tables: the PDF is probably scanned / unruled -> vision
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "gemini-3.5-flash")
EXTRACTION_FALLBACK_MODEL = os.getenv("EXTRACTION_FALLBACK_MODEL", "gemini-3-flash-preview")
KEY_ENV_VARS = ("EXTRACTION_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")

LABEL_TOKENS = ("libelle", "rubrique", "label", "element", "intitule", "description", "poste", "indicateur", "designation")
VALUE_TOKENS = ("valeur", "montant", "net", "brut", "value", "amount")


class ExtractionError(Exception):
    """Carries the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class ExtractedLine(BaseModel):
    """One QRT line as read by the vision model (structured output schema)."""
    code: str | None = Field(None, description="Row code exactly as printed, e.g. S.25.01-R0010 or R0010; null if none")
    label: str = Field(description="Row label")
    net: float | None = Field(None, description="Net amount column (C0030), no unit conversion")
    brut: float | None = Field(None, description="Gross amount column (C0040), no unit conversion")
    value: float | None = Field(None, description="Amount when the table has a single amount column")
    unit: str | None = Field(None, description="Unit as printed (M€, k€, %...)")


class ExtractedQrt(BaseModel):
    lines: list[ExtractedLine]


class ReportedModule(BaseModel):
    name: str = Field(description="Risk module, e.g. Marché, Souscription Non-Vie")
    value: float = Field(description="Capital requirement of that module (M EUR), as published")


class ReportedFigures(BaseModel):
    """Headline figures a prudential summary publishes (structured output of the summary reading)."""
    scr: float | None = Field(None, description="Published SCR")
    eligible_funds: float | None = Field(None, description="Published eligible own funds covering the SCR")
    mcr: float | None = Field(None, description="Published MCR")
    ratio_scr_pct: float | None = Field(None, description="Published SCR coverage ratio, in %")
    ratio_mcr_pct: float | None = Field(None, description="Published MCR coverage ratio, in %")
    bscr: float | None = Field(None, description="Published BSCR")
    diversification: float | None = Field(None, description="Published diversification effect (negative)")
    modules: list[ReportedModule] = Field(default_factory=list, description="Capital requirements per risk module, only if published")
    unit: str | None = Field(None, description="Unit of the amounts (M€, k€...)")


class QrtExtraction(BaseModel):
    sheets: dict[str, list[dict[str, Any]]]
    method: Literal["tables", "vision"]
    pages: int
    rows: int
    warnings: list[str] = Field(default_factory=list)
    model: str | None = Field(None, description="LLM used by the vision strategy")
    kind: Literal["qrt", "summary"] = Field("qrt", description="qrt: standard formula inputs; summary: a report that only publishes results")
    reported: ReportedFigures | None = Field(None, description="Published figures of a summary document (kind == summary)")


# ----------------------------------------------------------------------------- helpers


def _norm(text: Any) -> str:
    folded = unicodedata.normalize("NFD", str(text or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in folded if unicodedata.category(c) != "Mn")).strip()


def _clean(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace(" ", " ")).strip()


def parse_amount(text: str) -> float | None:
    """'1 234,5' -> 1234.5, '(12,3)' -> -12.3, '12,5 %' -> 12.5. None when it is not a plain number."""
    s = re.sub(r"[\s €%]|M€|k€", "", text.strip(), flags=re.IGNORECASE).replace("−", "-")
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return None
    number = float(s)
    return -number if negative else number


def _is_header(cells: list[str]) -> bool:
    heads = [_norm(c) for c in cells if c]
    has = lambda tokens: any(h.startswith(t) for h in heads for t in tokens)  # noqa: E731
    return len(heads) >= 2 and has(LABEL_TOKENS) and has(VALUE_TOKENS)


def _api_key() -> str | None:
    return next((os.environ[name] for name in KEY_ENV_VARS if os.getenv(name)), None)


# ----------------------------------------------------------------------------- strategy 1: ruled tables


def _cell_text(page: "pymupdf.Page", cell: Any) -> str:
    """Text of one table cell rebuilt from the words inside it. `Table.extract()` is not used for the text: it drops spaces
    and displaces punctuation ('5,5' -> '55 ,'), which would silently corrupt amounts."""
    if not cell:
        return ""
    x0, y0, x1, y1 = cell
    words = page.get_text("words", clip=pymupdf.Rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1))
    words.sort(key=lambda w: (round(w[1] / 4), w[0]))          # reading order: line by line, then left to right
    return _clean(" ".join(w[4] for w in words))


def _value_columns(headers: list[str]) -> set[str]:
    return {h for h in headers if h and any(_norm(h).startswith(t) for t in VALUE_TOKENS)}


def _record(headers: list[str], cells: list[str], value_cols: set[str]) -> dict[str, Any]:
    """One table row -> {original header: value}; amounts of the value columns become numbers, everything else stays text."""
    record: dict[str, Any] = {}
    for header, cell in zip(headers, cells):
        if not header or cell == "":
            continue
        amount = parse_amount(cell) if header in value_cols else None
        record[header] = amount if amount is not None else cell
    return record


def _orphan_rows(page: "pymupdf.Page", columns: list[Any], headers: list[str]) -> list[dict[str, Any]]:
    """Rows that spilled alone onto the next page: a one-row table is not detected as a table, so they are read with the
    column positions of the previous table. A line is only kept if it carries a real amount: a footer is never data."""
    words = page.get_text("words")
    words.sort(key=lambda w: (round(w[1] / 4), w[0]))
    lines: list[list[Any]] = []
    for w in words:
        if lines and abs(lines[-1][0][1] - w[1]) < 4:
            lines[-1].append(w)
        else:
            lines.append([w])
    value_cols = _value_columns(headers)
    rows: list[dict[str, Any]] = []
    for line in lines:
        cells = [""] * len(headers)
        for w in line:
            centre = (w[0] + w[2]) / 2
            idx = next((i for i, col in enumerate(columns) if col and col[0] - 2 <= centre <= col[1] + 2), None)
            if idx is not None:
                cells[idx] = (cells[idx] + " " + w[4]).strip()
        if _is_header(cells):
            continue                                   # header repeated at the top of the continuation page
        record = _record(headers, cells, value_cols)
        if sum(1 for c in cells if c) >= 2 and any(isinstance(record.get(h), float) for h in value_cols):
            rows.append(record)
        elif rows:
            break                                      # first non-data line after the rows: footer / notes, stop
    return rows


def _extract_tables(doc: "pymupdf.Document") -> dict[str, list[dict[str, Any]]]:
    merged: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    last: tuple[tuple[str, ...], list[Any]] | None = None     # (headers, column x-ranges) of the last QRT table: it may continue on the next page
    last_page = -10
    for page_no, page in enumerate(doc):
        try:
            tables = page.find_tables().tables           # used for the geometry (cells) only
        except Exception:                      # a page PyMuPDF cannot analyse must not sink the whole document
            continue
        for table_no, table in enumerate(tables):
            grid = [[_cell_text(page, cell) for cell in row.cells] for row in table.rows]
            start = next((i for i, row in enumerate(grid[:3]) if _is_header(row)), None)
            if start is None:
                # first table of the next page, same number of columns, no header: the previous QRT table continues
                if last and table_no == 0 and page_no == last_page + 1 and grid and len(grid[0]) == len(last[0]):
                    last_page = page_no
                    value_cols = _value_columns(list(last[0]))
                    for row in grid:
                        record = _record(list(last[0]), row, value_cols)
                        if record:
                            merged.setdefault(last[0], []).append(record)
                continue                       # otherwise: not a QRT table (cover page, legend...)
            headers, seen = [], {}
            for h in grid[start]:
                seen[h] = seen.get(h, 0) + 1
                headers.append(h if h and seen[h] == 1 else (f"{h} ({seen[h]})" if h else ""))
            last = (tuple(headers), [(c[0], c[2]) if c else None for c in table.rows[start].cells])
            last_page = page_no
            value_cols = _value_columns(headers)
            for row in grid[start + 1:]:
                if not any(row):
                    continue
                record = _record(headers, row, value_cols)
                if record:
                    merged.setdefault(tuple(headers), []).append(record)
        if not tables and last and page_no == last_page + 1:
            orphans = _orphan_rows(page, last[1], list(last[0]))
            if orphans:
                merged.setdefault(last[0], []).extend(orphans)
                last_page = page_no
    return {f"PDF_tableau_{i}": rows for i, rows in enumerate(merged.values(), start=1)}


# ----------------------------------------------------------------------------- strategy 2: Gemini vision

VISION_PROMPT = """Tu lis un état réglementaire Solvabilité II (QRT : S.23.01 fonds propres, S.25.01 SCR, S.26.xx modules de risque, S.28.01 MCR) fourni en PDF, éventuellement scanné.
Extrais CHAQUE ligne chiffrée de ces tableaux, dans l'ordre du document.
Règles :
- recopie le code de ligne tel qu'il est écrit (ex. S.25.01-R0010 ou R0010) ; n'en invente jamais, mets null s'il n'y en a pas ;
- `label` : le libellé de la ligne ;
- `net` et `brut` : montants des colonnes Net (C0030) et Brut (C0040) ; `value` : le montant s'il n'y a qu'une seule colonne de montants ;
- les montants sont des nombres recopiés SANS aucune conversion d'unité (un nombre entre parenthèses est négatif) ; `unit` : l'unité lue (M€, k€, %...) ;
- ignore les titres, notes de bas de page et lignes sans montant ; si un chiffre est illisible, mets null plutôt que de le deviner ;
- si le document ne contient aucun tableau QRT, renvoie une liste vide."""


def _gemini_structured(data: bytes, prompt: str, schema: type[BaseModel]) -> tuple[BaseModel, str]:
    """Sends the PDF to Gemini with a structured-output schema. Primary model, then fallback (503 'high demand' happens)."""
    from google import genai
    from google.genai import types

    key = _api_key()
    if not key:
        raise ExtractionError("Lecture IA indisponible : aucune clé Gemini configurée (rag_service/.env).", 503)
    client = genai.Client(api_key=key)
    config = types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=schema)
    part = types.Part.from_bytes(data=data, mime_type="application/pdf")
    errors: list[str] = []
    for model in dict.fromkeys(m for m in (EXTRACTION_MODEL, EXTRACTION_FALLBACK_MODEL) if m):
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model, contents=[part, prompt], config=config)
                parsed = response.parsed if isinstance(response.parsed, schema) else schema.model_validate_json(response.text or "{}")
                return parsed, model
            except Exception as exc:           # noqa: BLE001 - quota / 5xx / schema: try again, then next model
                errors.append(f"{model}: {str(exc)[:160]}")
                if any(code in str(exc) for code in ("404", "NOT_FOUND", "API_KEY", "PERMISSION", "INVALID_ARGUMENT")):
                    break
                time.sleep(2 * (attempt + 1))
    raise ExtractionError("Lecture IA du PDF impossible : " + " | ".join(errors[-2:]), 502)


def _vision_extract(data: bytes) -> tuple[list[ExtractedLine], str]:
    parsed, model = _gemini_structured(data, VISION_PROMPT, ExtractedQrt)
    return parsed.lines, model


SUMMARY_PROMPT = """Ce PDF est une synthèse prudentielle Solvabilité II (fiche d'analyse, rapport, tableau de bord) : il publie des RÉSULTATS déjà calculés, pas les états QRT.
Relève uniquement les chiffres explicitement publiés, y compris dans les encadrés et cartes de chiffres clés :
- scr : le SCR global ; eligible_funds : les fonds propres éligibles ; mcr : le MCR ;
- ratio_scr_pct et ratio_mcr_pct : les ratios de couverture publiés, en pourcentage (219.5 pour « 219,5 % ») ;
- bscr : le BSCR s'il est donné ; diversification : l'effet de diversification (nombre négatif) ;
- modules : les exigences de capital PAR MODULE DE RISQUE uniquement si elles sont publiées comme telles (ex. « SCR Marché 1 420 ») ;
- unit : l'unité des montants.
Règles strictes : n'utilise JAMAIS une valeur d'actif, de provision (Best Estimate), de marge de risque ou un total de bilan comme exigence de capital.
Ne calcule rien, ne déduis rien : un chiffre absent du document vaut null."""


def _vision_reported(data: bytes) -> tuple[ReportedFigures, str]:
    parsed, model = _gemini_structured(data, SUMMARY_PROMPT, ReportedFigures)
    return parsed, model


QRT_CODE = re.compile(r"S\.?\s?\d{2}\.?\s?\d{2}|\bR\d{4}\b|\bC\d{4}\b", re.IGNORECASE)


def has_qrt_codes(sheets: dict[str, list[dict[str, Any]]]) -> bool:
    """A standard-formula input carries QRT row codes (S.25.01, R0010...). A prudential summary does not."""
    return any(isinstance(v, str) and QRT_CODE.search(v)
               for rows in sheets.values() for row in rows for k, v in row.items() if _norm(k).startswith(("code", "ref")))


def _reported_sheet(f: ReportedFigures) -> list[dict[str, Any]]:
    """The published figures as preview rows (shown in the QRT table of the app)."""
    unit = f.unit or "M€"
    pairs = [("SCR", f.scr, unit), ("Fonds propres éligibles", f.eligible_funds, unit), ("MCR", f.mcr, unit),
             ("Ratio de couverture SCR", f.ratio_scr_pct, "%"), ("Ratio de couverture MCR", f.ratio_mcr_pct, "%"),
             ("BSCR", f.bscr, unit), ("Effet de diversification", f.diversification, unit)]
    pairs += [(f"SCR {m.name}", m.value, unit) for m in f.modules]
    return [{"Rubrique": label, "Valeur": value, "Unité": u} for label, value, u in pairs if value is not None]


def _lines_to_rows(lines: list[ExtractedLine]) -> list[dict[str, Any]]:
    rows = []
    for line in lines:
        amounts = {k: v for k, v in (("Montant Net (C0030)", line.net), ("Montant Brut (C0040)", line.brut), ("Valeur", line.value))
                   if v is not None and math.isfinite(v)}
        if not amounts or not line.label.strip():
            continue
        rows.append({"Code EIOPA": (line.code or "").strip(), "Rubrique": line.label.strip(), **amounts, "Unité": (line.unit or "").strip()})
    return rows


# ----------------------------------------------------------------------------- entry point


def extract_qrt_from_pdf(data: bytes, *, max_pages: int = MAX_PAGES) -> QrtExtraction:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ExtractionError(f"PDF illisible : {str(exc)[:120]}", 400) from exc
    if doc.page_count == 0:
        raise ExtractionError("Le PDF ne contient aucune page.", 400)
    if doc.page_count > max_pages:
        raise ExtractionError(f"PDF trop long ({doc.page_count} pages, maximum {max_pages}).", 413)

    sheets = _extract_tables(doc)
    n_table_rows = sum(len(rows) for rows in sheets.values())
    if n_table_rows >= MIN_TABLE_ROWS:
        result = QrtExtraction(sheets=sheets, method="tables", pages=doc.page_count, rows=n_table_rows)
    else:
        warnings = [f"Seulement {n_table_rows} ligne(s) lue(s) dans les tableaux du PDF : lecture par IA (vision)."] if n_table_rows else \
                   ["Aucun tableau QRT détecté dans le PDF (document scanné ou sans cadres) : lecture par IA (vision)."]
        try:
            lines, model = _vision_extract(data)
            rows = _lines_to_rows(lines)
        except ExtractionError as exc:          # the summary reading below still gets its chance before giving up
            rows, model, warnings = [], None, warnings + [exc.message]
        if rows:
            warnings.append("Valeurs lues par IA : vérifiez-les dans l'aperçu avant de calculer.")
            result = QrtExtraction(sheets={"PDF_QRT_vision": rows}, method="vision", pages=doc.page_count, rows=len(rows), warnings=warnings, model=model)
        else:                                  # keep the little the tables gave (may be empty: a pure summary is handled below)
            result = QrtExtraction(sheets=sheets, method="tables", pages=doc.page_count, rows=n_table_rows, warnings=warnings, model=model)
    return result if has_qrt_codes(result.sheets) else _as_summary(result, data)


def _as_summary(result: QrtExtraction, data: bytes) -> QrtExtraction:
    """No QRT code anywhere: the document publishes results, it does not hold standard-formula inputs. Recomputing an SCR
    from it would match asset values to risk charges by keyword, so its published figures are read instead."""
    note = "Document de synthèse, pas un état QRT : les chiffres publiés sont repris tels quels, la formule standard n'est pas recalculée."
    try:
        figures, model = _vision_reported(data)
    except ExtractionError as exc:
        if not result.rows:
            raise                                  # nothing at all to show: report why the AI reading failed (502 / 503)
        return result.model_copy(update={"kind": "summary", "warnings": result.warnings + [note, exc.message]})
    preview = _reported_sheet(figures)
    if not preview and not result.rows:
        raise ExtractionError("Aucune ligne QRT ni aucun chiffre prudentiel n'a pu être extrait de ce PDF.", 422)
    sheets = {**({"PDF_chiffres_reportes": preview} if preview else {}), **result.sheets}
    return result.model_copy(update={"kind": "summary", "reported": figures, "sheets": sheets, "model": result.model or model,
                                     "rows": sum(len(r) for r in sheets.values()), "warnings": result.warnings + [note]})
