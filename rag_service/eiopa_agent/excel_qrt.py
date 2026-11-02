"""Excel QRT generation (openpyxl). The files are saved on the server and are meant to be COMPUTED by the SCR agent.

The workbook mirrors the project's reference QRT files (qrt_pieuvre_scr/*.xlsx): the two INPUT sheets keep exactly their layout,

  S2501_SCR             Code EIOPA | Rubrique | Brut (C0040) | Net (C0030) | Unité      risk modules, op. risk, loss absorption
  Fonds_Propres_Ratios  Code EIOPA | Rubrique | Valeur | Unité                           Tier 1/2/3 and available own funds

so that "Importer QRT" -> "Calculer" (and the n8n engine) read them as they read any QRT. The result sheets of the reference files
(Pieuvre_SCR, Synthese, Matrices_Correlation) are NOT written: the SCR engine produces them and ignores them on input.
The agent's own explanatory sheets (Synthese_Instruments, Controles_Limites, Synthese_Sources) contain 'synthese' / 'controle' in their
name, which both the app and the engine skip, so they never end up read as data.
"""
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field

from eiopa_agent.own_funds import TIER_LABEL, Eligibility, OwnFundsPortfolio

GENERATED_DIR = Path(os.getenv("EIOPA_GENERATED_DIR", str(Path(__file__).resolve().parents[1] / "generated")))
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.xlsx$")
HEADER_FILL = PatternFill("solid", fgColor="1E3A8A")
OK_FILL, KO_FILL, CAP_FILL = PatternFill("solid", fgColor="D1FAE5"), PatternFill("solid", fgColor="FEE2E2"), PatternFill("solid", fgColor="FEF3C7")
MONEY = '#,##0.00;[Red]-#,##0.00'

# order matters: "non-vie" contains "vie"
MODULES = [("non-vie", "R0050", "Risque de souscription Non-Vie"), ("vie", "R0030", "Risque de souscription Vie"), ("santé", "R0040", "Risque de souscription Santé"),
           ("sante", "R0040", "Risque de souscription Santé"), ("marché", "R0010", "Risque de marché"), ("marche", "R0010", "Risque de marché"),
           ("contrepartie", "R0020", "Risque de défaut de contrepartie"), ("défaut", "R0020", "Risque de défaut de contrepartie")]
# details key -> (code, label, sign): absorption lines are negative in the reference files
DETAIL_LINES = [("diversificationBenefit", "R0060", "Diversification", -1), ("intangibles", "R0070", "Risque sur actifs incorporels", 1), ("bscr", "R0100", "SCR de base (BSCR)", 1),
                ("operationalRisk", "R0130", "Risque opérationnel", 1), ("lacTp", "R0140", "Absorption par les provisions techniques", -1),
                ("lacDt", "R0150", "Absorption par les impôts différés", -1)]


class GeneratedFile(BaseModel):
    name: str
    path: str
    template: str
    sheets: list[str]
    rows: int
    computable: bool = Field(False, description="True when the workbook holds the risk modules the SCR engine needs (S2501_SCR)")
    warnings: list[str] = Field(default_factory=list)


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list[Any]], widths: list[int], money_cols: tuple[int, ...] = ()) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill, cell.alignment = Font(bold=True, color="FFFFFF"), HEADER_FILL, Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    for col in money_cols:
        for cell in ws[get_column_letter(col)][1:]:
            cell.number_format = MONEY
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def s2501_rows(portfolio: OwnFundsPortfolio, solvency: dict[str, Any] | None) -> tuple[list[list[Any]], list[str]]:
    """S.25.01 input lines. SCR is intentionally not written: the SCR engine calculates it later."""
    warnings: list[str] = []
    modules = (solvency or {}).get("modules") or []
    details = (solvency or {}).get("details") or {}
    rows: list[list[Any]] = []
    seen: set[str] = set()
    for m in modules:
        key = str(m.get("name", "")).lower()
        net = _num(m.get("value", m.get("net")))
        hit = next(((c, label) for k, c, label in MODULES if k in key), None)
        if hit is None or net is None or hit[0] in seen:
            continue
        seen.add(hit[0])
        rows.append([f"S.25.01-{hit[0]}", hit[1], _num(m.get("brut")), round(net, 2), "M€"])
    if not rows:
        return [], ["Modules de risque S.25.01 absents : le fichier n'est pas calculable par l'agent SCR (aucun module à agréger)."]
    for key, code, label, sign in DETAIL_LINES:
        value = _num(details.get(key))
        if value is None:
            continue
        net = round(sign * abs(value), 2)                  # diversification and absorptions are negative, the other lines positive
        # lines without a mitigation effect carry the same amount gross and net, as in the reference files
        brut = net if key in ("intangibles", "operationalRisk", "lacTp", "lacDt") else None
        rows.append([f"S.25.01-{code}", label, brut, net, "M€"])
    rows.sort(key=lambda r: r[0])
    if _num(details.get("operationalRisk")) is None:
        warnings.append("Risque opérationnel (R0130) non fourni : l'agent SCR le comptera à 0.")
    return rows, warnings


def _own_funds_rows(e: Eligibility, portfolio: OwnFundsPortfolio) -> list[list[Any]]:
    """Write only own-funds inputs; SCR/MCR results are deliberately left to the SCR engine."""
    rows: list[list[Any]] = [
        ["S.23.01-R0020", "Tier 1 non restreint", e.t1_unrestricted, "M€"], ["S.23.01-R0030", "Tier 1 restreint", e.t1_restricted, "M€"],
        ["S.23.01-R0040", "Tier 2 (dettes subordonnées)", e.t2, "M€"], ["S.23.01-R0050", "Tier 3 (DTA nets)", e.t3, "M€"],
        ["S.23.01-R0060", "Fonds propres disponibles totaux", e.available, "M€"],
    ]
    return rows


def _sources_sheet(wb: Workbook, sources: list[dict[str, Any]], rule_confirmed: bool | None, evidence_ids: list[int], question: str) -> None:
    ws = wb.create_sheet("Synthese_Sources")
    verdict = "non évalué" if rule_confirmed is None else ("OUI" if rule_confirmed else "NON")
    ws.append(["Règle confirmée par la base documentaire (jugement de l'agent, preuves vérifiées)", verdict])
    ws.append(["Passages cités comme preuves", ", ".join(f"[{i}]" for i in evidence_ids) or "aucun"])
    ws.append(["Pertinence maximale des passages retrouvés", max((s["score"] for s in sources), default=0)])
    ws.append(["Généré le", datetime.now().strftime("%d/%m/%Y %H:%M")])
    ws.append(["Question", question[:300]])
    ws.append([])
    ws.append(["N°", "Source", "Page", "Pertinence", "Preuve", "Extrait"])
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
    for s in sources:
        ws.append([s["id"], s["source"], s["page"], s["score"], "oui" if s["id"] in evidence_ids else "", s["excerpt"]])
    for r in range(1, 6):
        ws.cell(row=r, column=1).font = Font(bold=True)
    for letter, w in zip("ABCDEF", (48, 44, 10, 12, 8, 100)):
        ws.column_dimensions[letter].width = w
    if rule_confirmed is not True:
        ws.append([])
        ws.append(["Les limites appliquées sont celles de la formule standard intégrées à l'outil ; la base documentaire ne les confirme pas explicitement."])


def write_qrt(template: str, portfolio: OwnFundsPortfolio, eligibility: Eligibility, sources: list[dict[str, Any]], rule_confirmed: bool | None,
              evidence_ids: list[int] | None = None, question: str = "", solvency: dict[str, Any] | None = None) -> GeneratedFile:
    if template not in ("S.23.01", "S.25.01"):
        raise ValueError(f"Modèle QRT inconnu : {template} (disponibles : S.23.01, S.25.01).")
    scr_rows, warnings = s2501_rows(portfolio, solvency)
    if template == "S.25.01" and not scr_rows:
        raise ValueError("S.25.01 nécessite les modules de risque du calcul SCR (modules, détails) dans la requête.")

    wb = Workbook()
    wb.remove(wb.active)
    if scr_rows:                                           # same sheet order as the reference files: S2501_SCR first, then the own funds
        _sheet(wb, "S2501_SCR", ["Code EIOPA", "Rubrique", "Brut (C0040)", "Net (C0030)", "Unité"], scr_rows, [17, 46, 15, 15, 9], money_cols=(3, 4))
    fonds = _own_funds_rows(eligibility, portfolio)
    _sheet(wb, "Fonds_Propres_Ratios", ["Code EIOPA", "Rubrique", "Valeur", "Unité"], fonds, [17, 51, 13, 9], money_cols=(3,))
    _sheet(wb, "Synthese_Instruments", ["Instrument", "Classe (Tier)", "Nature", "Montant (M€)"],
           [[i.name, TIER_LABEL[i.tier], i.kind or "", i.amount] for i in portfolio.items] + [["Total", "", "", eligibility.available]], [46, 22, 24, 16], money_cols=(4,))
    ctrl_rows = [[c.name, c.rule, c.threshold, c.value, c.status, c.note] for c in eligibility.checks]
    _sheet(wb, "Controles_Limites", ["Contrôle", "Règle", "Seuil", "Valeur", "Statut", "Commentaire"], ctrl_rows, [34, 46, 12, 12, 12, 56], money_cols=(3, 4))
    ws = wb["Controles_Limites"]
    for row in ws.iter_rows(min_row=2):
        row[4].fill = {"OK": OK_FILL, "PLAFONNÉ": CAP_FILL}.get(row[4].value, KO_FILL)
    ws.append([])
    ws.append(["PLAFONNÉ : plafond d'éligibilité dépassé, l'excédent n'est pas éligible (ce n'est pas une infraction). ÉCART : minimum non atteint."])
    ws.append(["Critères qualitatifs de classement (subordination, durée, absence d'incitation au remboursement) : NON vérifiés par cet outil."])
    _sources_sheet(wb, sources, rule_confirmed, evidence_ids or [], question)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    name = f"QRT_{template.replace('.', '')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.xlsx"
    path = GENERATED_DIR / name
    wb.save(path)
    return GeneratedFile(name=name, path=str(path), template=template, sheets=wb.sheetnames, rows=len(scr_rows) + len(fonds), computable=bool(scr_rows), warnings=warnings)
