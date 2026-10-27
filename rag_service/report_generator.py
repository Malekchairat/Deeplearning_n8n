"""SCR audit report: payload -> pie chart (matplotlib) -> HTML (jinja2) -> PDF (xhtml2pdf).

xhtml2pdf has no flexbox/grid and ignores border-radius, so the layout is built with <table>s.
"""
import base64
import io
from datetime import datetime

from jinja2 import Environment
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure   # Figure API (not pyplot): safe to call from FastAPI worker threads
from xhtml2pdf import pisa

PALETTE = ["#1E3A8A", "#2563EB", "#3B82F6", "#60A5FA", "#64748B", "#94A3B8"]
SEVERITY = {
    "high": {"label": "CRITIQUE", "fg": "#B91C1C", "bg": "#FEE2E2"},
    "medium": {"label": "MODÉRÉE", "fg": "#C2410C", "bg": "#FFEDD5"},
    "low": {"label": "NORMALE", "fg": "#047857", "bg": "#D1FAE5"},
}
SUBSTITUTIONS = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "≈": "~", "×": "x", "−": "-", "’": "'", " ": " ", " ": " "}


def clean(value) -> str:
    """The built-in PDF fonts only cover cp1252: swap look-alikes and drop what cannot be drawn (emojis...)."""
    text = "" if value is None else str(value)
    for src, dst in SUBSTITUTIONS.items():
        text = text.replace(src, dst)
    return text.encode("cp1252", "ignore").decode("cp1252")


def num(value, default=0.0) -> float:
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else default
    except (TypeError, ValueError):
        return default


def fmt(value, digits=1) -> str:
    """French number format: 1 234,5"""
    return f"{num(value):,.{digits}f}".replace(",", " ").replace(".", ",")


def severity_of(value) -> str:
    s = str(value or "").lower()
    if any(k in s for k in ("high", "critic", "élev", "elev", "haut", "severe")):
        return "high"
    if any(k in s for k in ("med", "moder", "modér", "moyen")):
        return "medium"
    return "low"


def pie_chart_b64(modules) -> str | None:
    items = [(clean(m.get("name", "")), num(m.get("value")), m.get("color")) for m in modules if num(m.get("value")) > 0]
    fig = Figure(figsize=(8.4, 3.6), dpi=150)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    if not items:
        ax.text(0.5, 0.5, "Aucune donnée de décomposition", ha="center", va="center", fontsize=12, color="#64748B")
        ax.axis("off")
    else:
        names, values, colors = zip(*items)
        colors = [c if isinstance(c, str) and c.startswith("#") else PALETTE[i % len(PALETTE)] for i, c in enumerate(colors)]
        total = sum(values)
        wedges, _ = ax.pie(values, colors=colors, startangle=90, counterclock=False, wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 2})
        ax.text(0, 0.1, f"{fmt(total)} M€", ha="center", va="center", fontsize=12, fontweight="bold", color="#0F172A")
        ax.text(0, -0.17, "somme des modules", ha="center", va="center", fontsize=7.5, color="#64748B")
        ax.legend(wedges, [f"{n} : {fmt(v)} M€ ({v / total * 100:.0f} %)" for n, v in zip(names, values)],
                  loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False, fontsize=9.5)
        ax.set_aspect("equal")
    fig.subplots_adjust(left=0.02, right=0.5 if items else 0.98, top=0.98, bottom=0.02)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=False, facecolor="white")
    return base64.b64encode(buf.getvalue()).decode("ascii")


TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
  @page { size: A4; margin: 1.6cm 1.5cm 2cm 1.5cm;
    @frame footer_frame { -pdf-frame-content: footer; left: 1.5cm; width: 18cm; top: 28cm; height: 1cm; } }
  body { font-family: Helvetica; font-size: 10pt; color: #0F172A; }
  h2 { font-size: 13pt; color: #1E3A8A; margin: 18px 0 6px 0; border-bottom: 1px solid #CBD5E1; padding-bottom: 3px; }
  table { border-collapse: collapse; width: 100%; }
  .head td { background-color: #1E3A8A; color: #FFFFFF; padding: 14px 16px; }
  .head .title { font-size: 19pt; font-weight: bold; }
  .head .sub { font-size: 9pt; color: #BFDBFE; }
  .head .right { text-align: right; font-size: 9pt; color: #BFDBFE; }
  .badge { background-color: {{ sev.bg }}; color: {{ sev.fg }}; border: 1px solid {{ sev.fg }}; padding: 5px 10px; font-weight: bold; font-size: 9pt; text-align: center; }
  .summary td { padding: 10px 12px; background-color: #F8FAFC; border: 1px solid #E2E8F0; font-size: 10.5pt; }
  .kpi td { border: 1px solid #E2E8F0; padding: 10px 8px; text-align: center; width: 25%; }
  .kpi .label { font-size: 8pt; color: #64748B; }
  .kpi .value { font-size: 15pt; font-weight: bold; margin-top: 4px; }
  .grid th { background-color: #E2E8F0; color: #334155; font-size: 8.5pt; text-align: left; padding: 6px 8px; border: 1px solid #CBD5E1; }
  .grid td { padding: 6px 8px; border: 1px solid #E2E8F0; vertical-align: top; font-size: 9.5pt; }
  .evidence { font-family: Courier; background-color: #F1F5F9; }
  .action td { background-color: #EFF6FF; border: 1px solid #93C5FD; border-left: 5px solid #2563EB; padding: 10px 12px; }
  .action .tag { font-size: 8pt; font-weight: bold; color: #1D4ED8; }
  .muted { color: #64748B; font-size: 9pt; }
  .warn td { background-color: #FFFBEB; border: 1px solid #FCD34D; padding: 6px 10px; font-size: 9pt; color: #92400E; }
</style></head>
<body>
<table class="head"><tr>
  <td><div class="title">Rapport d'audit SCR</div><div class="sub">SolvaRisk - Solvabilité II, formule standard</div></td>
  <td class="right">Généré le {{ generated }}<br/>Statut réglementaire : <b>{{ regulatory }}</b></td>
</tr></table>

<h2>Synthèse de l'analyste</h2>
{% if analyst %}
<table><tr>
  <td style="width: 3cm; vertical-align: top;"><table><tr><td class="badge">{{ sev.label }}</td></tr></table></td>
  <td style="padding-left: 10px; vertical-align: middle;" class="muted">Gravité du diagnostic{% if analyst_status %} - {{ analyst_status }}{% endif %}</td>
</tr></table>
<table class="summary" style="margin-top: 8px;"><tr><td>{{ analyst.summary }}</td></tr></table>
{% else %}
<table class="warn"><tr><td>Aucun diagnostic de l'analyste n'est joint à ce rapport.</td></tr></table>
{% endif %}

<h2>Indicateurs clés</h2>
<table class="kpi"><tr>
  <td><div class="label">FONDS PROPRES ÉLIGIBLES</div><div class="value" style="color: #1E3A8A;">{{ eligible }} M€</div></td>
  <td><div class="label">SCR</div><div class="value" style="color: #047857;">{{ scr }} M€</div></td>
  <td><div class="label">MCR</div><div class="value" style="color: #B45309;">{{ mcr }} M€</div></td>
  <td><div class="label">RATIO DE SOLVABILITÉ</div><div class="value" style="color: {{ ratio_color }};">{{ ratio }} %</div></td>
</tr></table>

<h2>Répartition du SCR par module de risque</h2>
<div class="muted">Charges nettes par module, avant bénéfice de diversification.</div>
<div style="text-align: center; margin-top: 6px;">
  {% if pie %}<img src="data:image/png;base64,{{ pie }}" width="480"/>{% endif %}
</div>
<table class="grid" style="margin-top: 6px;">
  <tr><th>Module</th><th style="text-align: right;">Charge (M€)</th><th style="text-align: right;">Part</th></tr>
  {% for m in modules %}<tr><td>{{ m.name }}</td><td style="text-align: right;">{{ m.value }}</td><td style="text-align: right;">{{ m.share }} %</td></tr>{% endfor %}
</table>

{% if analyst %}
<h2>Constats détaillés</h2>
{% if analyst.findings %}
<table class="grid">
  <tr><th style="width: 4.2cm;">Indicateur</th><th style="width: 3cm;">Constat</th><th>Interprétation</th></tr>
  {% for f in analyst.findings %}<tr><td><b>{{ f.indicator }}</b></td><td class="evidence">{{ f.evidence }}</td><td>{{ f.interpretation }}</td></tr>{% endfor %}
</table>
{% else %}<p class="muted">Aucun constat détaillé.</p>{% endif %}

{% if analyst.recommended_action %}
<h2>Action recommandée</h2>
<table class="action"><tr><td><div class="tag">ACTION RECOMMANDÉE</div><div style="margin-top: 4px; font-size: 10.5pt;">{{ analyst.recommended_action }}</div></td></tr></table>
{% endif %}
{% endif %}

{% if warnings %}
<h2>Points de contrôle du moteur</h2>
<table class="warn">{% for w in warnings %}<tr><td>{{ w }}</td></tr>{% endfor %}</table>
{% endif %}

<div id="footer" class="muted" style="text-align: center; border-top: 1px solid #CBD5E1; padding-top: 4px;">
  SolvaRisk - Document généré automatiquement, à valider par un actuaire - page <pdf:pagenumber/> / <pdf:pagecount/>
</div>
</body></html>"""

_env = Environment(autoescape=True)   # analyst text comes from an LLM: always escaped
_template = _env.from_string(TEMPLATE)


def build_report_pdf(scr_data: dict, analyst_data: dict | None = None) -> bytes:
    scr_data = scr_data or {}
    ratio = num(scr_data.get("ratio"))
    details = scr_data.get("details") if isinstance(scr_data.get("details"), dict) else {}
    modules_raw = [m for m in (scr_data.get("modules") or []) if isinstance(m, dict)]
    total = sum(max(num(m.get("value")), 0) for m in modules_raw) or 1.0

    analyst = None
    if isinstance(analyst_data, dict) and str(analyst_data.get("summary", "")).strip():
        findings = [f for f in (analyst_data.get("findings") or []) if isinstance(f, dict)]
        analyst = {
            "summary": clean(analyst_data["summary"]),
            "findings": [{"indicator": clean(f.get("indicator")), "evidence": clean(f.get("evidence")), "interpretation": clean(f.get("interpretation"))} for f in findings],
            "recommended_action": clean(analyst_data.get("recommended_action") or analyst_data.get("recommendedAction")),
        }
    sev = SEVERITY[severity_of((analyst_data or {}).get("severity"))]
    regulatory = details.get("regulatoryStatus") or ("BREACH_SCR" if ratio < 100 else "WATCHLIST" if ratio < 130 else "COMPLIANT")

    html = _template.render(
        generated=datetime.now().strftime("%d/%m/%Y à %H:%M"),
        regulatory=clean(regulatory), sev=sev, analyst=analyst, analyst_status=clean((analyst_data or {}).get("status", "")).replace("_", " "),
        eligible=fmt(scr_data.get("eligibleFunds")), scr=fmt(scr_data.get("scr")), mcr=fmt(scr_data.get("mcr")), ratio=fmt(ratio, 2),
        ratio_color="#B91C1C" if ratio < 100 else "#C2410C" if ratio < 130 else "#047857",
        pie=pie_chart_b64(modules_raw),
        modules=[{"name": clean(m.get("name")), "value": fmt(m.get("value")), "share": fmt(max(num(m.get("value")), 0) / total * 100, 0)} for m in modules_raw],
        warnings=[clean(w) for w in (scr_data.get("warnings") or []) if w][:8],
    )
    out = io.BytesIO()
    result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"PDF rendering failed ({result.err} errors)")
    return out.getvalue()
