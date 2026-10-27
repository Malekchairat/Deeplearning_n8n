"""Tests of the PDF -> QRT extraction (no network: the vision model is stubbed). Run: python -m pytest extraction/tests -q"""
import io

import pymupdf
import pytest
from fastapi.testclient import TestClient
from xhtml2pdf import pisa

from extraction import qrt_pdf
from extraction.qrt_pdf import ExtractedLine, ExtractionError, ReportedFigures, ReportedModule, extract_qrt_from_pdf, has_qrt_codes, parse_amount

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test may reach Gemini: both readings fail unless a test stubs them explicitly."""
    def unavailable(_data):
        raise ExtractionError("offline (test)", 502)
    monkeypatch.setattr(qrt_pdf, "_vision_extract", unavailable)
    monkeypatch.setattr(qrt_pdf, "_vision_reported", unavailable)


STYLE = "<style>table{border-collapse:collapse} td,th{border:1px solid #000;padding:4px;font-size:10pt}</style>"


def html_pdf(body: str) -> bytes:
    out = io.BytesIO()
    pisa.CreatePDF(f"<html><head>{STYLE}</head><body>{body}</body></html>", dest=out)
    return out.getvalue()


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


QRT_ROWS = [["S.25.01-R0010", "Risque de marché", "108,73", "M€"],
            ["S.25.01-R0060", "Diversification", "(112,42)", "M€"],
            ["S.25.01-R0100", "SCR de base (BSCR)", "1 250,48", "M€"],
            ["S.25.01-R0200", "SCR", "251,17", "M€"]]
QRT_PDF = html_pdf(table(["Code EIOPA", "Rubrique", "Montant Net (C0030)", "Unité"], QRT_ROWS))


def scanned(pdf: bytes) -> bytes:
    """Same page rendered to an image: no text layer, no vector table (what a scan looks like)."""
    pix = pymupdf.open(stream=pdf, filetype="pdf")[0].get_pixmap(dpi=110)
    out = pymupdf.open()
    page = out.new_page(width=pix.width, height=pix.height)
    page.insert_image(page.rect, pixmap=pix)
    return out.tobytes()


def stub_vision(monkeypatch, lines=None, error: ExtractionError | None = None):
    calls = []

    def fake(data):
        calls.append(len(data))
        if error:
            raise error
        return lines or [], "stub-model"

    monkeypatch.setattr(qrt_pdf, "_vision_extract", fake)
    if error:                                      # the same outage hits the summary reading
        monkeypatch.setattr(qrt_pdf, "_vision_reported", lambda data: (_ for _ in ()).throw(error))
    return calls


# ----------------------------------------------------------------------------- numbers


@pytest.mark.parametrize("text, expected", [("108,73", 108.73), ("1 234,5", 1234.5), ("1.234,56", 1234.56), ("(12,3)", -12.3),
                                            ("−5", -5.0), ("12,5 %", 12.5), ("325", 325.0), ("S.25.01", None), ("", None), ("n/a", None)])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


# ----------------------------------------------------------------------------- strategy 1: tables


def test_ruled_tables_are_read_without_calling_the_llm(monkeypatch):
    calls = stub_vision(monkeypatch)
    result = extract_qrt_from_pdf(QRT_PDF)
    assert result.method == "tables" and not calls and result.model is None
    rows = next(iter(result.sheets.values()))
    assert len(rows) == 4 and result.rows == 4 and result.pages == 1
    by_code = {r["Code EIOPA"]: r for r in rows}
    assert by_code["S.25.01-R0010"]["Montant Net (C0030)"] == 108.73
    assert by_code["S.25.01-R0060"]["Montant Net (C0030)"] == -112.42           # French parentheses -> negative
    assert by_code["S.25.01-R0100"]["Montant Net (C0030)"] == 1250.48           # 'thousands space' + decimal comma
    assert by_code["S.25.01-R0200"]["Rubrique"] == "SCR" and by_code["S.25.01-R0200"]["Unité"] == "M€"


def test_codes_stay_text_and_only_value_columns_become_numbers():
    rows = next(iter(extract_qrt_from_pdf(html_pdf(table(["Code", "Libellé", "Valeur", "Unité"], [["0010", "A", "5,5", "M€"], ["0020", "B", "7", "M€"], ["0030", "C", "9", "M€"]]))).sheets.values()))
    assert rows[0]["Code"] == "0010" and rows[0]["Valeur"] == 5.5


def test_a_table_that_is_not_a_qrt_is_ignored(monkeypatch):
    stub_vision(monkeypatch, error=ExtractionError("no key", 503))
    with pytest.raises(ExtractionError) as err:
        extract_qrt_from_pdf(html_pdf(table(["Nom", "Age", "Ville"], [["A", "1", "X"], ["B", "2", "Y"], ["C", "3", "Z"]])))
    assert err.value.status == 503


# ----------------------------------------------------------------------------- strategy 2: vision


def test_scanned_pdf_goes_to_vision_and_returns_the_excel_shape(monkeypatch):
    lines = [ExtractedLine(code="S.25.01-R0010", label="Risque de marché", net=108.73, brut=120.0, unit="M€"),
             ExtractedLine(code=None, label="Fonds propres éligibles", value=372.09, unit="M€"),
             ExtractedLine(label="Ligne sans montant"),                           # dropped
             ExtractedLine(label="", net=1.0)]                                     # dropped (no label)
    calls = stub_vision(monkeypatch, lines)
    result = extract_qrt_from_pdf(scanned(QRT_PDF))
    assert calls and result.method == "vision" and result.model == "stub-model" and result.rows == 2
    rows = result.sheets["PDF_QRT_vision"]
    assert rows[0] == {"Code EIOPA": "S.25.01-R0010", "Rubrique": "Risque de marché", "Montant Net (C0030)": 108.73, "Montant Brut (C0040)": 120.0, "Unité": "M€"}
    assert rows[1]["Valeur"] == 372.09 and rows[1]["Code EIOPA"] == ""
    assert any("vérifiez" in w for w in result.warnings)                         # AI reading is always flagged


def test_vision_with_no_line_is_a_422(monkeypatch):
    stub_vision(monkeypatch, [])
    monkeypatch.setattr(qrt_pdf, "_vision_reported", lambda data: (ReportedFigures(), "stub"))   # no figure either
    with pytest.raises(ExtractionError) as err:
        extract_qrt_from_pdf(scanned(QRT_PDF))
    assert err.value.status == 422


def test_partial_tables_are_kept_when_vision_is_down(monkeypatch):
    stub_vision(monkeypatch, error=ExtractionError("503 high demand", 502))
    pdf = html_pdf(table(["Code EIOPA", "Rubrique", "Montant Net (C0030)"], QRT_ROWS[:2]))   # 2 rows < MIN_TABLE_ROWS
    result = extract_qrt_from_pdf(pdf)
    assert result.method == "tables" and result.rows == 2 and any("503" in w for w in result.warnings)


# ----------------------------------------------------------------------------- bad inputs


def test_unreadable_and_oversized_documents():
    with pytest.raises(ExtractionError) as err:
        extract_qrt_from_pdf(b"not a pdf at all")
    assert err.value.status == 400
    with pytest.raises(ExtractionError) as err:
        extract_qrt_from_pdf(QRT_PDF, max_pages=0)
    assert err.value.status == 413


def test_vision_needs_a_key(monkeypatch):
    for name in qrt_pdf.KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ExtractionError) as err:
        qrt_pdf._gemini_structured(b"%PDF", "prompt", qrt_pdf.ExtractedQrt)
    assert err.value.status == 503


# ----------------------------------------------------------------------------- HTTP


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


def test_endpoint_returns_sheets(client, monkeypatch):
    stub_vision(monkeypatch)
    response = client.post("/api/extract/qrt-pdf", files={"file": ("qrt.pdf", QRT_PDF, "application/pdf")})
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "tables" and body["rows"] == 4 and "PDF_tableau_1" in body["sheets"]


def test_endpoint_rejects_non_pdf_and_oversized(client, monkeypatch):
    assert client.post("/api/extract/qrt-pdf", files={"file": ("data.xlsx", b"PK", "application/vnd.ms-excel")}).status_code == 400
    monkeypatch.setattr("extraction.api.MAX_BYTES", 100)
    assert client.post("/api/extract/qrt-pdf", files={"file": ("big.pdf", QRT_PDF, "application/pdf")}).status_code == 413


def test_endpoint_maps_extraction_errors(client, monkeypatch):
    stub_vision(monkeypatch, error=ExtractionError("modèle indisponible", 502))
    response = client.post("/api/extract/qrt-pdf", files={"file": ("scan.pdf", scanned(QRT_PDF), "application/pdf")})
    assert response.status_code == 502 and "indisponible" in response.json()["detail"]


# ----------------------------------------------------------------------------- multi-page


def test_a_table_split_over_two_pages_is_reassembled():
    rows = [[f"S.25.01-R{i:04d}", f"Ligne {i}", f"{i},50", "M€"] for i in range(1, 61)]            # long enough to overflow a page
    pdf = html_pdf(table(["Code EIOPA", "Rubrique", "Montant Net (C0030)", "Unité"], rows))
    assert len(pymupdf.open(stream=pdf, filetype="pdf")) >= 2
    result = extract_qrt_from_pdf(pdf)
    assert result.method == "tables" and result.rows == 60 and len(result.sheets) == 1            # no row lost at the page break
    assert next(iter(result.sheets.values()))[-1]["Code EIOPA"] == "S.25.01-R0060"


def test_a_single_row_spilling_onto_the_next_page_is_not_lost():
    """Real layout of a two-table QRT (12 + 15 lines): the last line lands alone on page 2, where PyMuPDF sees no table."""
    style = "<style>td,th{border:1px solid #000;padding:8px;font-size:9pt}</style>"          # tall rows: the 27th lands alone on page 2
    scr = table(["Code EIOPA", "Rubrique", "Brut (C0040)", "Net (C0030)", "Unité"], [[f"S.25.01-R{i:04d}", f"Ligne {i}", f"{i},10", f"{i},20", "M€"] for i in range(1, 13)])
    funds = table(["Code EIOPA", "Rubrique", "Valeur", "Unité"], [[f"S.23.01-R{i:04d}", f"Fonds {i}", f"{i},30", "M€"] for i in range(1, 15)] + [["RATIO", "Ratio de couverture MCR", "238,08", "%"]])
    out = io.BytesIO()
    pisa.CreatePDF(f"<html><head>{style}</head><body><h3>QRT</h3>{scr}<br/>{funds}</body></html>", dest=out)
    doc = pymupdf.open(stream=out.getvalue(), filetype="pdf")
    assert len(doc) == 2 and len(doc[1].find_tables().tables) == 0 and "238,08" in doc[1].get_text()      # the layout this test is about
    result = extract_qrt_from_pdf(out.getvalue())
    assert result.rows == 27 and result.method == "tables"
    last = list(result.sheets.values())[-1][-1]
    assert last == {"Code EIOPA": "RATIO", "Rubrique": "Ratio de couverture MCR", "Valeur": 238.08, "Unité": "%"}


def test_a_page_footer_is_never_taken_for_a_row():
    pdf = html_pdf(table(["Code EIOPA", "Rubrique", "Montant Net (C0030)"], QRT_ROWS) + '<p style="page-break-before:always">Document confidentiel - page 2 / 2</p>')
    assert extract_qrt_from_pdf(pdf).rows == 4


# ----------------------------------------------------------------------------- prudential summaries (not a QRT)

SUMMARY_TABLE = table(["Poste d'Actif, Passif & Capital Requis", "Valeur (M€)", "Quote-part"],
                      [["Actions cotées et Participations stratégiques", "3 692", "13.0% du total actif"],
                       ["Best Estimate Passif Non-Vie (Sinistres et Primes)", "5 800", "20.4% du bilan"],
                       ["Fonds Propres Éligibles (EOF)", "4 720", "16.6% du bilan"],
                       ["SCR Global Solvabilité II", "2 150", "Excédent net : +2 570 M€"],
                       ["Effet Diversification Inter-Modules (BSCR)", "-640", "-22.9%"]])
FIGURES = ReportedFigures(scr=2150, eligible_funds=4720, mcr=983, ratio_scr_pct=219.5, ratio_mcr_pct=480.2, diversification=-640,
                          modules=[ReportedModule(name="Marché", value=1420)], unit="M€")


def test_qrt_code_detection():
    assert has_qrt_codes({"s": [{"Code EIOPA": "S.25.01-R0010"}]}) and has_qrt_codes({"s": [{"code": "S.23.01.01-R0500-C0020"}]})
    assert has_qrt_codes({"s": [{"Code": "R0100"}]})
    assert not has_qrt_codes({"s": [{"Rubrique": "SCR Global", "Valeur": 2150}]}) and not has_qrt_codes({"s": [{"Code": "0010"}]})


def test_a_table_without_qrt_codes_is_read_as_a_summary_not_recomputed(monkeypatch):
    monkeypatch.setattr(qrt_pdf, "_vision_reported", lambda data: (FIGURES, "stub-model"))
    result = extract_qrt_from_pdf(html_pdf(SUMMARY_TABLE))
    assert result.kind == "summary" and result.reported == FIGURES and result.model == "stub-model"
    assert list(result.sheets)[0] == "PDF_chiffres_reportes"                                  # published figures first in the preview
    preview = {r["Rubrique"]: r["Valeur"] for r in result.sheets["PDF_chiffres_reportes"]}
    assert preview["SCR"] == 2150 and preview["MCR"] == 983 and preview["SCR Marché"] == 1420 and preview["Ratio de couverture SCR"] == 219.5
    assert any("pas un état QRT" in w for w in result.warnings)


def test_a_summary_without_any_table_is_read_from_its_key_figures(monkeypatch):
    monkeypatch.setattr(qrt_pdf, "_vision_extract", lambda data: ([], "stub"))                  # no QRT line anywhere
    monkeypatch.setattr(qrt_pdf, "_vision_reported", lambda data: (FIGURES, "stub-model"))
    result = extract_qrt_from_pdf(html_pdf("<h1>Ratio SCR 219,5 %</h1><p>SCR 2 150 M€, fonds propres 4 720 M€, MCR 983 M€.</p>"))
    assert result.kind == "summary" and list(result.sheets) == ["PDF_chiffres_reportes"] and result.reported.scr == 2150


def test_summary_reading_down_keeps_the_rows_and_says_so():
    result = extract_qrt_from_pdf(html_pdf(SUMMARY_TABLE))                                   # autouse fixture: Gemini unavailable
    assert result.kind == "summary" and result.reported is None and result.rows == 5
    assert any("offline" in w for w in result.warnings) and any("pas un état QRT" in w for w in result.warnings)


def test_a_real_qrt_never_triggers_the_summary_reading(monkeypatch):
    called = []
    monkeypatch.setattr(qrt_pdf, "_vision_reported", lambda data: called.append(1))
    assert extract_qrt_from_pdf(QRT_PDF).kind == "qrt" and not called
