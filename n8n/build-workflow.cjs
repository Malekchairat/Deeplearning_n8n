// Builds n8n/solvarisk-workflow.json from the exported "My workflow.json": keeps the SCR engine and the Gmail credential,
// fixes what was broken and adds the PDF route.   Usage: node n8n/build-workflow.cjs "<path to My workflow.json>"
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const src = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const byName = (n) => { const node = src.nodes.find((x) => x.name === n); if (!node) throw new Error("node not found: " + n); return node; };
const uid = () => crypto.randomUUID();

// ---------------------------------------------------------------- 1. SCR engine (kept as is, two fixes)
const scrHook = byName("Webhook scr");
const engine = byName("Code in JavaScript");
engine.name = "Moteur SCR v2.2";
// n8n's Code node cannot require('xlsx'): the app builds the Excel itself from `sheets`, so skip the doomed export (and its warning)
engine.parameters.jsCode = engine.parameters.jsCode.replace("watchlist: 130, exportXlsx: true,", "watchlist: 130, exportXlsx: false,");
if (!engine.parameters.jsCode.includes("exportXlsx: false")) throw new Error("engine patch failed");
engine.onError = "continueErrorOutput";            // a thrown error (e.g. no QRT rows) leaves through output 2 instead of hanging the webhook
const respondScr = byName("Respond to Webhook");
respondScr.name = "Réponse SCR";

const respondJsonError = (name, code, pos, fallback) => ({
  parameters: { respondWith: "json", responseBody: `={{ { status: 'error', message: String($json.error?.message ?? $json.error ?? '${fallback}') } }}`, options: { responseCode: code } },
  type: "n8n-nodes-base.respondToWebhook", typeVersion: 1.5, position: pos, id: uid(), name,
});
const scrError = respondJsonError("Erreur SCR (422)", 422, [208, 176], "Erreur du moteur SCR");

// ---------------------------------------------------------------- 2. Audit PDF route -> Python container
const pdfHook = { parameters: { httpMethod: "POST", path: "report-pdf", responseMode: "responseNode", options: {} }, type: "n8n-nodes-base.webhook", typeVersion: 2.2, position: [-240, 400], id: uid(), name: "Webhook Report PDF", webhookId: uid() };
const pdfCall = {
  parameters: {
    method: "POST", url: "http://report-pdf:8002/report", sendBody: true, specifyBody: "json", jsonBody: "={{ JSON.stringify($json.body) }}",
    options: { timeout: 60000, response: { response: { responseFormat: "file", outputPropertyName: "data" } } },
  },
  type: "n8n-nodes-base.httpRequest", typeVersion: 4.5, position: [-16, 400], id: uid(), name: "Python PDF Generator", onError: "continueErrorOutput",
};
const pdfRespond = {
  parameters: {
    respondWith: "binary", responseDataSource: "set", inputFieldName: "data",
    options: { responseHeaders: { entries: [{ name: "Content-Type", value: "application/pdf" }, { name: "Content-Disposition", value: 'attachment; filename="Rapport_Audit_SolvaRisk.pdf"' }] } },
  },
  type: "n8n-nodes-base.respondToWebhook", typeVersion: 1.5, position: [208, 384], id: uid(), name: "Réponse PDF",
};
const pdfError = respondJsonError("Erreur PDF (502)", 502, [208, 560], "Le service PDF est indisponible");

// ---------------------------------------------------------------- 3. Notifications (Gmail)
const notifyHook = byName("Webhook Notification");
notifyHook.parameters.responseMode = "onReceived";   // the old responseNode mode never answered: the caller waited until timeout
const sw = byName("Switch");
sw.name = "Type de notification";
sw.parameters.options = { fallbackOutput: "extra" };  // unknown types are ignored explicitly instead of silently dropped
const unknown = { parameters: {}, type: "n8n-nodes-base.noOp", typeVersion: 1, position: [272, 1040], id: uid(), name: "Type inconnu (ignoré)" };

const MAIL_COMMON = [
  "// The webhook is open: only addresses of these domains are accepted, otherwise the default recipient is used",
  "const ALLOWED_DOMAINS = ['esprit.tn'];",
  "const DEFAULT_RECIPIENT = 'ahmed.tounsi@esprit.tn';",
  "const body = $json.body ?? $json;",
  "const d = body.details ?? {};",
  "const esc = (v) => String(v ?? '').replace(/[&<>\"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', \"'\": '&#39;' }[c]));",
  "const fix = (v, n = 2) => (v !== undefined && v !== null && v !== '' && Number.isFinite(Number(v)) ? Number(v).toFixed(n) : 'N/A');",
  "const wanted = String(body.recipient ?? '').trim().toLowerCase();",
  "const recipient = /^[^@\\s]+@[^@\\s]+$/.test(wanted) && ALLOWED_DOMAINS.includes(wanted.split('@')[1]) ? wanted : DEFAULT_RECIPIENT;",
  "const subject = String(body.subject ?? '').replace(/[\\r\\n]+/g, ' ').trim().slice(0, 150);",
  "const row = (label, value) => '<li><strong>' + label + ' :</strong> ' + value + '</li>';",
  "const footer = '<p style=\"margin-top:20px;font-size:12px;color:#718096;\">Notification générée automatiquement par le pipeline SolvaRisk.</p>';",
].join("\n") + "\n";

const alertCode = byName("Code in JavaScript1");
alertCode.name = "Mail – alerte";
alertCode.parameters = {
  mode: "runOnceForEachItem",
  jsCode: MAIL_COMMON + [
    "const items = [",
    "  row('Statut qualité des données', esc(d.quality_status ?? 'N/A') + (d.quality_score !== undefined ? ' (score ' + fix(d.quality_score, 1) + '/100)' : '')),",
    "  row('Anomalies bloquantes', esc(d.critical_count ?? 0)),",
    "  row('Ratio de solvabilité', d.ratio !== undefined ? fix(d.ratio) + ' %' : 'N/A'),",
    "  d.severity ? row('Gravité du diagnostic', esc(d.severity)) : '',",
    "].join('');",
    "const summary = d.summary ? '<p><em>' + esc(String(d.summary).slice(0, 600)) + '</em></p>' : '';",
    "const action = d.recommended_action ? '<p><strong>Action recommandée :</strong> ' + esc(String(d.recommended_action).slice(0, 400)) + '</p>' : '';",
    "const html = '<div style=\"font-family:Arial,sans-serif;color:#222;\"><h2 style=\"color:#c53030;\">Alerte Solvabilité II &ndash; Anomalie détectée</h2><p>Une anomalie critique nécessite votre attention immédiate :</p><ul>' + items + '</ul>' + summary + action + footer + '</div>';",
    "return { json: { recipient, subject: '[SolvaRisk – ALERTE] ' + (subject || 'Incident critique'), html } };",
  ].join("\n"),
};
alertCode.position = [272, 720];

const reportCode = {
  parameters: {
    mode: "runOnceForEachItem",
    jsCode: MAIL_COMMON + [
      "const items = [",
      "  row('Ratio de solvabilité', d.ratio !== undefined ? fix(d.ratio) + ' %' : 'N/A'),",
      "  row('SCR', d.scr !== undefined ? fix(d.scr) + ' M€' : 'N/A'),",
      "  row('Fonds propres éligibles', d.eligible_funds !== undefined ? fix(d.eligible_funds) + ' M€' : 'N/A'),",
      "  row('MCR', d.mcr !== undefined ? fix(d.mcr) + ' M€' : 'N/A'),",
      "  d.severity ? row('Gravité du diagnostic', esc(d.severity)) : '',",
      "  d.report_name ? row('Rapport', esc(d.report_name)) : '',",
      "].join('');",
      "const html = '<div style=\"font-family:Arial,sans-serif;color:#222;\"><h2 style=\"color:#1e3a8a;\">Rapport d&rsquo;audit SCR prêt</h2><p>Le rapport PDF a été généré et est disponible dans SolvaRisk.</p><ul>' + items + '</ul>' + footer + '</div>';",
      "return { json: { recipient, subject: '[SolvaRisk] ' + (subject || \"Rapport d'audit SCR prêt\"), html } };",
    ].join("\n"),
  },
  type: "n8n-nodes-base.code", typeVersion: 2, position: [272, 880], id: uid(), name: "Mail – rapport prêt",
};

const gmail = byName("Send Email");
gmail.parameters = { sendTo: "={{ $json.recipient }}", subject: "={{ $json.subject }}", message: "={{ $json.html }}", options: { appendAttribution: false } };
gmail.position = [496, 800];

// ---------------------------------------------------------------- 4. Documentation note
const note = {
  parameters: {
    width: 600, height: 320,
    content: [
      "## SolvaRisk – webhooks utilisés par l'application",
      "**POST /webhook/scr-agent** : corps `{ sheets | data }` -> moteur SCR (JSON). Erreur -> 422 `{status:'error'}`.",
      "**POST /webhook/report-pdf** : corps `{ scrData, analystData }` -> PDF (binaire) généré par le conteneur `report-pdf:8002`. Erreur -> 502.",
      "**POST /webhook/notify** : corps `{ type: 'alert' | 'report_ready', recipient?, subject?, details }` -> répond tout de suite puis envoie un e-mail Gmail.",
      "Destinataires acceptés : domaine esprit.tn, sinon destinataire par défaut (voir les nœuds « Mail »).",
      "",
      "Pré-requis : le conteneur `n8n` doit être sur le réseau Docker `solva-net` (docker network connect solva-net n8n).",
    ].join("\n"),
  },
  type: "n8n-nodes-base.stickyNote", typeVersion: 1, position: [-300, -300], id: uid(), name: "Notes",
};

// ---------------------------------------------------------------- positions & connections
scrHook.position = [-240, 16]; engine.position = [-16, 16]; respondScr.position = [208, 0];
notifyHook.position = [-240, 800]; sw.position = [-16, 800];
const link = (node, ...outs) => ({ [node]: { main: outs.map((o) => o.map((n) => ({ node: n, type: "main", index: 0 }))) } });
const connections = {
  ...link("Webhook scr", ["Moteur SCR v2.2"]),
  ...link("Moteur SCR v2.2", ["Réponse SCR"], ["Erreur SCR (422)"]),
  ...link("Webhook Report PDF", ["Python PDF Generator"]),
  ...link("Python PDF Generator", ["Réponse PDF"], ["Erreur PDF (502)"]),
  ...link("Webhook Notification", ["Type de notification"]),
  ...link("Type de notification", ["Mail – alerte"], ["Mail – rapport prêt"], ["Type inconnu (ignoré)"]),
  ...link("Mail – alerte", ["Send Email"]),
  ...link("Mail – rapport prêt", ["Send Email"]),
};

const out = {
  name: "SolvaRisk – Pipeline n8n",
  nodes: [scrHook, engine, respondScr, scrError, pdfHook, pdfCall, pdfRespond, pdfError, notifyHook, sw, alertCode, reportCode, unknown, gmail, note],
  pinData: {}, connections, active: false,
  settings: { executionOrder: "v1", binaryMode: "separate" },
  meta: { templateCredsSetupCompleted: true }, tags: [],
};
const dest = path.join(__dirname, "solvarisk-workflow.json");
fs.writeFileSync(dest, JSON.stringify(out, null, 2), "utf8");
console.log("written", dest, "-", out.nodes.length, "nodes");
