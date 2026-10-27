"""The generated workbook must be 'like the reference QRT file' AND computable by the SCR agent.

1. fidelity   : rebuild the reference workbook (qrt_pieuvre_scr/2_Mutuelle_IARD_Dominante.xlsx) from its own figures and compare
                the two INPUT sheets cell by cell (headers, codes, labels, units, amounts);
2. computable : run the generated workbook through the REAL n8n engine (the code of 'Moteur SCR v2.2' in n8n/solvarisk-workflow.json)
                and check it returns the same SCR, own funds and ratios as for the reference file.
Both skip when the reference file / node are not available.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from openpyxl import load_workbook

from eiopa_agent import excel_qrt
from eiopa_agent.own_funds import OwnFundsItem, OwnFundsPortfolio, compute_eligibility

ROOT = Path(__file__).resolve().parents[3]
REFERENCE = ROOT / "qrt_pieuvre_scr" / "2_Mutuelle_IARD_Dominante.xlsx"
WORKFLOW = ROOT / "n8n" / "solvarisk-workflow.json"
NODE = shutil.which("node")
IGNORED = re.compile(r"pieuvre|synthese|matric|controle|result", re.I)       # sheets the app and the engine skip on input

pytestmark = pytest.mark.skipif(not REFERENCE.exists() or not WORKFLOW.exists(), reason="reference QRT file or n8n workflow not found")


@pytest.fixture(autouse=True)
def tmp_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_qrt, "GENERATED_DIR", tmp_path / "generated")


def table(wb, name):
    ws = wb[name]
    heads = [c.value for c in ws[1]]
    return [dict(zip(heads, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(v is not None for v in r)]


def portfolio_from_reference():
    """The agent's inputs, taken from the reference workbook itself."""
    wb = load_workbook(REFERENCE, data_only=True)
    s25, fonds = {r["Code EIOPA"]: r for r in table(wb, "S2501_SCR")}, {r["Code EIOPA"] + "|" + r["Rubrique"]: r for r in table(wb, "Fonds_Propres_Ratios")}
    val = lambda code: next(r["Valeur"] for k, r in fonds.items() if k.startswith(code))           # noqa: E731
    pf = OwnFundsPortfolio(scr=s25["S.25.01-R0200"]["Net (C0030)"], mcr=val("S.28.01-R0310"), mcr_linear=val("S.28.01-R0010"), amcr=val("S.28.01-R0330"), items=[
        OwnFundsItem(name="Tier 1 non restreint", tier="T1_UNRESTRICTED", amount=val("S.23.01-R0020")), OwnFundsItem(name="Tier 1 restreint", tier="T1_RESTRICTED", amount=val("S.23.01-R0030")),
        OwnFundsItem(name="Tier 2", tier="T2", amount=val("S.23.01-R0040")), OwnFundsItem(name="Tier 3", tier="T3", amount=val("S.23.01-R0050"))])
    solvency = {"modules": [{"name": s25[c]["Rubrique"], "value": s25[c]["Net (C0030)"], "brut": s25[c]["Brut (C0040)"]} for c in ("S.25.01-R0010", "S.25.01-R0020", "S.25.01-R0030", "S.25.01-R0040", "S.25.01-R0050")],
                "details": {"diversificationBenefit": s25["S.25.01-R0060"]["Net (C0030)"], "intangibles": s25["S.25.01-R0070"]["Net (C0030)"], "bscr": s25["S.25.01-R0100"]["Net (C0030)"],
                            "operationalRisk": s25["S.25.01-R0130"]["Net (C0030)"], "lacTp": s25["S.25.01-R0140"]["Net (C0030)"], "lacDt": s25["S.25.01-R0150"]["Net (C0030)"]}}
    return pf, solvency


def generate():
    pf, solvency = portfolio_from_reference()
    return excel_qrt.write_qrt("S.23.01", pf, compute_eligibility(pf), [], None, solvency=solvency)


def test_input_sheets_are_the_reference_ones_cell_by_cell():
    ref, gen = load_workbook(REFERENCE, data_only=True), load_workbook(generate().path, data_only=True)
    for sheet in ("S2501_SCR", "Fonds_Propres_Ratios"):
        assert [c.value for c in gen[sheet][1]] == [c.value for c in ref[sheet][1]], f"{sheet}: headers differ"
        g, r = table(gen, sheet), table(ref, sheet)
        assert [(x["Code EIOPA"], x["Rubrique"], x["Unité"]) for x in g] == [(x["Code EIOPA"], x["Rubrique"], x["Unité"]) for x in r], f"{sheet}: lines differ"
        amount_cols = [h for h in r[0] if h not in ("Code EIOPA", "Rubrique", "Unité")]
        for gx, rx in zip(g, r):
            for col in amount_cols:
                if gx[col] is not None:                                                    # gross amounts of result lines (BSCR, SCR...) are not invented
                    assert gx[col] == pytest.approx(rx[col], abs=0.011), f"{sheet} {gx['Code EIOPA']} {col}"
    assert [s for s in gen.sheetnames if not IGNORED.search(s)] == ["S2501_SCR", "Fonds_Propres_Ratios"]     # same input sheets, same order


def test_the_generated_file_is_flagged_computable():
    f = generate()
    assert f.computable and f.warnings == [] and f.sheets[:2] == ["S2501_SCR", "Fonds_Propres_Ratios"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_real_n8n_engine_computes_the_generated_workbook(tmp_path):
    code = next(n["parameters"]["jsCode"] for n in json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"] if n["name"] == "Moteur SCR v2.2")
    harness = tmp_path / "harness.cjs"
    harness.write_text("""const fs=require('fs');const AF=Object.getPrototypeOf(async function(){}).constructor;
const {code,sheets}=JSON.parse(fs.readFileSync(0,'utf8'));
new AF('$input',code)({first:()=>({json:{sheets,options:{exportXlsx:false}}})}).then(([o])=>{const j=o.json;
console.log(JSON.stringify({scr:j.scr,eligibleFunds:j.eligibleFunds,mcr:j.mcr,ratio:j.ratio,status:j.details.regulatoryStatus,recon:j.quality.reconciliation,warnings:j.quality.warnings}))
}).catch(e=>console.log(JSON.stringify({error:e.message})));""", encoding="utf-8")

    def run_engine(path):
        wb = load_workbook(path, data_only=True)
        sheets = {name: table(wb, name) for name in wb.sheetnames if name in ("S2501_SCR", "Fonds_Propres_Ratios") or not IGNORED.search(name)}
        out = subprocess.run([NODE, str(harness)], input=json.dumps({"code": code, "sheets": sheets}), capture_output=True, text=True, encoding="utf-8", timeout=60)
        return json.loads(out.stdout)

    generated, reference = run_engine(generate().path), run_engine(REFERENCE)
    assert "error" not in generated, generated
    for key in ("scr", "eligibleFunds", "mcr", "ratio"):
        assert generated[key] == pytest.approx(reference[key], abs=0.011), key
    assert (generated["scr"], generated["eligibleFunds"], generated["status"]) == (251.17, 372.09, "COMPLIANT")
    assert all(r["Statut"] == "OK" for r in generated["recon"]), generated["recon"]       # computed SCR agrees with the reported R0200
    assert generated["warnings"] == []


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_without_risk_modules_the_file_is_honestly_not_computable():
    pf, _ = portfolio_from_reference()
    f = excel_qrt.write_qrt("S.23.01", pf, compute_eligibility(pf), [], None)             # no solvency data at all
    assert not f.computable and "pas calculable" in f.warnings[0]
    assert load_workbook(f.path).sheetnames[0] == "Fonds_Propres_Ratios"                  # no S2501_SCR sheet to mislead the engine
