import * as XLSX from "xlsx";

export type AgentName = "Document Agent" | "SCR Agent" | "RAG Agent" | "Risk Analyst Agent" | "Report Agent";

/** Ligne plate, utilisée uniquement pour l'aperçu dans l'interface. */
export type QrtRow = { code: string; libelle: string; valeur: number; unite: string };

/** Contenu brut du classeur Excel : une entrée par feuille, en-têtes d'origine conservés. */
export type QrtSheets = Record<string, Record<string, unknown>[]>;

export type ScrResult = {
  eligibleFunds: number;
  scr: number;
  mcr: number;
  ratio: number;
  modules: { name: string; value: number; color: string }[];
  // Champs supplémentaires renvoyés par le moteur n8n (v2.1)
  details?: Record<string, unknown>;
  tree?: Record<string, unknown>[];
  sheets?: Record<string, Record<string, unknown>[]>;
  quality?: { warnings: string[]; reconciliation: Record<string, unknown>[]; engine: string };
  status?: string;
};

export type ScrOptions = {
  tolerancePct?: number;
  amcrDefault?: number;
  taxRate?: number;
  lacTpRate?: number;
  watchlist?: number;
  exportXlsx?: boolean;
};

export const webhookPaths: Record<string, string> = {
  "rag-agent": process.env.N8N_WEBHOOK_RAG_AGENT ?? "/webhook/rag-agent",
  "ingest-course": process.env.N8N_WEBHOOK_INGEST_COURSE ?? "/webhook/ingest-course",
  "scr-agent": process.env.N8N_WEBHOOK_SCR_AGENT ?? "/webhook/scr-agent",
  "risk-analyst": process.env.N8N_WEBHOOK_RISK_ANALYST ?? "/webhook/risk-analyst",
  "risk-agent": process.env.N8N_WEBHOOK_RISK_AGENT ?? "/webhook/risk-agent",
  "report-agent": process.env.N8N_WEBHOOK_REPORT_AGENT ?? "/webhook/report-agent",   // Stress test tab: JSON { report }
  "report-pdf": process.env.N8N_WEBHOOK_REPORT_PDF ?? "/webhook/report-pdf",         // audit PDF: binary, see route.ts
  notify: process.env.N8N_WEBHOOK_NOTIFY ?? "/webhook/notify",                       // Gmail alerts: { type: 'alert' | 'report_ready', details }
};

// ---------------------------------------------------------------------------
// Données de démonstration (mode simulation uniquement)
// ---------------------------------------------------------------------------
export const sampleQrt: QrtRow[] = [
  { code: "S.23.01", libelle: "Fonds propres de base", valeur: 185.4, unite: "M€" },
  { code: "S.23.01", libelle: "Fonds propres éligibles", valeur: 172.8, unite: "M€" },
  { code: "S.25.01", libelle: "SCR standard", valeur: 9.4, unite: "M€" },
  { code: "S.25.01", libelle: "MCR", valeur: 43.8, unite: "M€" },
  { code: "S.25.01", libelle: "Ajustement capacité absorption pertes", valeur: -8.2, unite: "M€" },
];

/** DÉMO : renvoie des valeurs fictives. À n'utiliser qu'en mode simulation. */
export function calculateScr(rows: QrtRow[] = sampleQrt): ScrResult {
  const eligibleFunds = rows.find((row) => row.libelle.toLowerCase().includes("éligibles"))?.valeur ?? 172.8;
  const scr = rows.find((row) => row.libelle.toLowerCase() === "scr standard")?.valeur ?? 96.4;
  const mcr = rows.find((row) => row.libelle.toLowerCase() === "mcr")?.valeur ?? 43.8;
  const base = [42.1, 28.6, 16.8, 13.9, 11.6];
  const modules = [
    ["Marché", base[0], "#2563EB"],
    ["Souscription Non-Vie", base[1], "#3B82F6"],
    ["Vie", base[2], "#475569"],
    ["Santé", base[3], "#64748B"],
    ["Contrepartie", base[4], "#94A3B8"],
  ].map(([name, value, color]) => ({ name: String(name), value: Number(value), color: String(color) }));
  return { eligibleFunds, scr, mcr, ratio: (eligibleFunds / scr) * 100, modules, status: "simulation" };
}

// ---------------------------------------------------------------------------
// Lecture de l'Excel
// ---------------------------------------------------------------------------

/** Feuilles produites par le moteur (sorties) : jamais envoyées en entrée. */
const OUTPUT_SHEET = /pieuvre|synthese|matric|controle|result/i;

const normText = (s: unknown) =>
  String(s ?? "").trim().toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");

const toNumber = (v: unknown): number | null => {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (v === null || v === undefined) return null;
  let s = String(v).trim().replace(/[\s €%]/g, "");
  if (!s) return null;
  const neg = /^\(.*\)$/.test(s);
  s = s.replace(/[()]/g, "");
  if (s.includes(",") && s.includes(".")) {
    s = s.lastIndexOf(",") > s.lastIndexOf(".") ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
  } else {
    s = s.replace(",", ".");
  }
  const n = Number(s);
  return Number.isFinite(n) ? (neg ? -n : n) : null;
};

/**
 * Lit TOUTES les feuilles du classeur en conservant les en-têtes d'origine
 * (Code EIOPA, Rubrique, Brut (C0040), Net (C0030), Valeur, Unité…).
 * C'est ce format que le moteur n8n attend.
 */
export function parseQrtWorkbook(buffer: ArrayBuffer): QrtSheets {
  const wb = XLSX.read(buffer, { type: "array" });
  if (!wb.SheetNames.length) throw new Error("Le fichier Excel ne contient aucune feuille.");
  const sheets: QrtSheets = {};
  for (const name of wb.SheetNames) {
    const grid = XLSX.utils.sheet_to_json<unknown[]>(wb.Sheets[name], { header: 1, defval: null, raw: true });
    // La ligne d'en-têtes = première ligne avec au moins 2 cellules renseignées
    const h = grid.findIndex((r) => r.filter((c) => c !== null && c !== "").length >= 2);
    if (h < 0) continue;
    const headers = grid[h].map((c) => String(c ?? "").trim());
    const rows = grid
      .slice(h + 1)
      .filter((r) => r.some((c) => c !== null && c !== ""))
      .map((r) => Object.fromEntries(headers.map((k, i) => [k, r[i] ?? null]).filter(([k]) => k)));
    if (rows.length) sheets[name] = rows;
  }
  if (!Object.keys(sheets).length) throw new Error("Aucune ligne exploitable n'a été trouvée dans le fichier Excel.");
  return sheets;
}

/**
 * Aperçu plat (code / libellé / valeur / unité) construit depuis les feuilles d'entrée.
 * Valeur retenue : Net, sinon Valeur, sinon Brut. Sert à l'affichage uniquement.
 */
export function qrtRowsFromSheets(sheets: QrtSheets): QrtRow[] {
  const out: QrtRow[] = [];
  for (const [name, rows] of Object.entries(sheets)) {
    if (OUTPUT_SHEET.test(name)) continue;
    for (const row of rows) {
      const keys = Object.keys(row);
      const find = (re: RegExp) => keys.find((k) => re.test(normText(k)));
      const kCode = find(/^code/);
      const kLabel = find(/^(libelle|rubrique|label|nom|name)/);
      const kNet = find(/^net/);
      const kBrut = find(/^brut/);
      const kVal = find(/^(valeur|value|montant)$/);
      const kUnit = find(/^unit/);
      const valeur = toNumber(kNet && row[kNet]) ?? toNumber(kVal && row[kVal]) ?? toNumber(kBrut && row[kBrut]);
      if (valeur === null) continue;
      out.push({
        code: String(kCode ? row[kCode] ?? "" : "").trim(),
        libelle: String(kLabel ? row[kLabel] ?? "" : "").trim(),
        valeur,
        unite: String(kUnit ? row[kUnit] ?? "M€" : "M€").trim() || "M€",
      });
    }
  }
  if (!out.length) throw new Error("Aucune ligne QRT valide n'a été trouvée (colonnes Libellé/Rubrique et Valeur/Net/Brut attendues).");
  return out;
}

/** Compatibilité : lit le fichier et renvoie l'aperçu plat. */
export function parseQrtFile(buffer: ArrayBuffer): QrtRow[] {
  return qrtRowsFromSheets(parseQrtWorkbook(buffer));
}

// ---------------------------------------------------------------------------
// Envoi au webhook n8n
// ---------------------------------------------------------------------------

/**
 * Construit le corps de la requête pour /webhook/scr-agent.
 * On n'envoie que les feuilles QRT d'entrée (S2501_SCR, Fonds_Propres_Ratios, S.26.xx…).
 */
export function buildScrPayload(
  sheets: QrtSheets,
  opts: { periode?: string; simulation?: boolean; options?: ScrOptions } = {},
) {
  const input = Object.fromEntries(Object.entries(sheets).filter(([name]) => !OUTPUT_SHEET.test(name)));
  if (!Object.keys(input).length) {
    throw new Error("Aucune feuille QRT d'entrée trouvée (attendu : S2501_SCR, Fonds_Propres_Ratios…).");
  }
  // Garde-fou : refuse d'envoyer un fichier dont tous les montants sont absents ou nuls
  const hasAmount = Object.values(input).some((rows) =>
    rows.some((r) => Object.entries(r).some(([k, v]) => /net|brut|valeur|montant|value/.test(normText(k)) && (toNumber(v) ?? 0) !== 0)),
  );
  if (!hasAmount) throw new Error("Tous les montants du fichier sont vides ou à 0 : vérifiez les colonnes Net / Brut / Valeur.");
  return {
    sheets: input,
    periode: opts.periode,
    simulation: opts.simulation ?? false,
    ...(opts.options ? { options: opts.options } : {}),
  };
}

/** Garde-fou sur la réponse : détecte un résultat vide plutôt que d'afficher un faux « BREACH_MCR ». */
export function assertValidScrResult(result: ScrResult): ScrResult {
  if (!Number.isFinite(result.scr) || result.scr <= 0) {
    const detail = result.quality?.warnings?.slice(0, 3).join(" | ");
    throw new Error(`Le moteur a renvoyé un SCR nul : les données reçues sont probablement vides.${detail ? ` (${detail})` : ""}`);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Export Excel du résultat
// ---------------------------------------------------------------------------

export function downloadOutput(result: ScrResult) {
  const workbook = XLSX.utils.book_new();

  // Résultat complet du moteur n8n (Synthese, Pieuvre_SCR, S2501_SCR, Fonds_Propres_Ratios, Controles)
  if (result.sheets && Object.keys(result.sheets).length) {
    for (const [name, rows] of Object.entries(result.sheets)) {
      XLSX.utils.book_append_sheet(
        workbook,
        XLSX.utils.json_to_sheet(rows.length ? rows : [{ Info: "vide" }]),
        name.slice(0, 31),
      );
    }
  } else {
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet([
      { Indicateur: "Fonds propres éligibles", Valeur: result.eligibleFunds, Unité: "M€" },
      { Indicateur: "SCR standard", Valeur: result.scr, Unité: "M€" },
      { Indicateur: "MCR", Valeur: result.mcr, Unité: "M€" },
      { Indicateur: "Ratio de solvabilité", Valeur: result.ratio, Unité: "%" },
    ]), "Synthèse SCR");
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(result.modules.map((module) => ({
      Module: module.name, Charge: module.value, Unité: "M€",
    }))), "BSCR Modules");
  }
  XLSX.writeFile(workbook, "SolvaRisk_Output.xlsx");
}

// ---------------------------------------------------------------------------
// Réponses simulées (sans appel à n8n)
// ---------------------------------------------------------------------------

export function simulationReply(agent: string, body: Record<string, unknown>) {
  if (agent === "rag-agent") return { answer: "Selon le cadre **Solvabilité II**, le ratio de solvabilité compare les fonds propres éligibles au SCR : $Ratio = Eligible\\ Funds / SCR$. Une diversification maîtrisée réduit le BSCR agrégé via la matrice de corrélation.", source: "Collection solvabilite_2_rules", body };
  if (agent === "risk-agent") {
    const ratio = Number(body.ratio);
    const critical = Number.isFinite(ratio) && ratio < 100;
    const watch = Number.isFinite(ratio) && ratio < 130;
    const shown = Number.isFinite(ratio) ? `${ratio.toFixed(2)} %` : "n/d";
    return {
      status: critical ? "anomaly_detected" : watch ? "watchlist" : "normal",
      severity: critical ? "high" : watch ? "medium" : "low",
      summary: critical
        ? `Le ratio de solvabilité (${shown}) est inférieur au seuil réglementaire de 100 % : le SCR n'est plus couvert par les fonds propres éligibles.`
        : watch
          ? `Le ratio de solvabilité (${shown}) reste conforme mais sous le seuil de surveillance de 130 %.`
          : `Le ratio de solvabilité (${shown}) est confortable et aucun signal d'alerte n'est détecté.`,
      findings: [{ indicator: "Ratio SCR", evidence: shown, interpretation: critical ? "Brèche réglementaire constatée." : "Couverture du SCR conforme à l'exigence de 100 %." }],
      recommended_action: critical ? "Déclencher un plan de rétablissement et informer le superviseur." : "Poursuivre la surveillance trimestrielle des fonds propres éligibles.",
    };
  }
  if (agent === "risk-analyst") return { interpretation: "Le scénario reste absorbable : le choc actions est le principal contributeur à la baisse du ratio. La couverture du SCR demeure supérieure au seuil réglementaire de 100 %.", severity: "Modérée" };
  if (agent === "report-agent") return { report: "## Note actuarielle — Synthèse du stress test\n\nLe portefeuille conserve une marge de solvabilité positive. Le risque de marché domine l’évolution du ratio ; une surveillance trimestrielle des fonds propres éligibles est recommandée." };
  return { ...calculateScr(), status: "simulation" };
}