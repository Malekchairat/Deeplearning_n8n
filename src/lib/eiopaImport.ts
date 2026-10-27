import type { QrtSheets } from "@/lib/solvarisk";

export type Tier = "T1_UNRESTRICTED" | "T1_RESTRICTED" | "T2" | "T3";
export type ModuleKey = "marche" | "defaut" | "vie" | "sante" | "nonvie";

export type ImportedEiopa = {
  scr: number | null;
  mcr: number | null;
  mcrLinear: number | null;
  amcr: number | null;
  items: { name: string; tier: Tier; amount: number }[];
  modules: Partial<Record<ModuleKey, { net: number | null; brut: number | null }>>;
  details: { intangibles: number | null; operationalRisk: number | null; lacTp: number | null; lacDt: number | null };
  warnings: string[];
};

// S.25.01 module lines and S.23.01 Tier lines, as in the project's reference QRT workbooks (qrt_pieuvre_scr/*.xlsx)
const MODULE_CODES: Record<ModuleKey, string> = { marche: "S2501R0010", defaut: "S2501R0020", vie: "S2501R0030", sante: "S2501R0040", nonvie: "S2501R0050" };
const TIER_CODES: { tier: Tier; code: string; label: RegExp }[] = [
  { tier: "T1_UNRESTRICTED", code: "S2301R0020", label: /tier ?1.*non restreint|unrestricted/ },
  { tier: "T1_RESTRICTED", code: "S2301R0030", label: /tier ?1.*restreint|restricted/ },
  { tier: "T2", code: "S2301R0040", label: /tier ?2/ },
  { tier: "T3", code: "S2301R0050", label: /tier ?3/ },
];
const IGNORED_SHEET = /pieuvre|synthese|matric|controle|result/i;

const fold = (s: unknown) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

const toNumber = (v: unknown): number | null => {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string" || !v.trim()) return null;
  const n = Number(v.replace(/[\s €%]/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : null;
};

type Row = Record<string, unknown>;
const pick = (row: Row, prefixes: string[]) => {
  const key = Object.keys(row).find((k) => prefixes.some((p) => fold(k).startsWith(p)));
  return key === undefined ? undefined : row[key];
};
const codeKey = (row: Row) => String(pick(row, ["code"]) ?? "").toUpperCase().replace(/[\s.\-]/g, "");
const labelOf = (row: Row) => String(pick(row, ["rubrique", "libelle", "label", "element"]) ?? "").trim();
// Net first, then a single 'Valeur' column, then Gross: the same preference as the SCR engine
const amountOf = (row: Row) => toNumber(pick(row, ["net", "montant net"])) ?? toNumber(pick(row, ["valeur", "value", "montant"])) ?? toNumber(pick(row, ["brut", "montant brut"]));

/** Reads a QRT workbook like the reference files and returns what the EIOPA agent form needs. Never throws: gaps become warnings. */
export function importEiopaFromSheets(sheets: QrtSheets): ImportedEiopa {
  const rows = Object.entries(sheets).filter(([name]) => !IGNORED_SHEET.test(name)).flatMap(([, list]) => list as Row[]);
  const byCode = (code: string) => rows.find((r) => codeKey(r) === code);
  const warnings: string[] = [];

  const items: ImportedEiopa["items"] = [];
  for (const { tier, code, label } of TIER_CODES) {
    const row = byCode(code) ?? rows.find((r) => label.test(fold(labelOf(r))) && !/eligible|disponible|total/.test(fold(labelOf(r))) && (tier !== "T1_RESTRICTED" || !/non restreint|unrestricted/.test(fold(labelOf(r)))));
    const amount = row ? amountOf(row) : null;
    if (row && amount !== null) items.push({ name: labelOf(row) || tier, tier, amount });
  }
  if (!items.length) warnings.push("Aucune ligne de fonds propres par Tier (S.23.01) n'a été trouvée dans ce classeur.");

  const modules: ImportedEiopa["modules"] = {};
  for (const [key, code] of Object.entries(MODULE_CODES) as [ModuleKey, string][]) {
    const row = byCode(code);
    if (row) modules[key] = { net: toNumber(pick(row, ["net"])) ?? toNumber(pick(row, ["valeur", "value", "montant"])), brut: toNumber(pick(row, ["brut"])) };
  }
  const detail = (code: string) => { const row = byCode(code); const v = row ? amountOf(row) : null; return v === null ? null : Math.abs(v); };
  const scrRow = byCode("S2501R0200") ?? byCode("S2501R0220");
  const scr = scrRow ? amountOf(scrRow) : null;
  if (scr === null) warnings.push("SCR (S.25.01-R0200) introuvable : saisissez-le à la main.");
  if (!Object.keys(modules).length) warnings.push("Aucun module de risque S.25.01 : le fichier généré ne sera pas calculable par l'agent SCR.");
  const val = (code: string) => { const row = byCode(code); return row ? amountOf(row) : null; };

  return {
    scr, mcr: val("S2801R0310") ?? val("S2801R0400"), mcrLinear: val("S2801R0010"), amcr: val("S2801R0330"), items, modules,
    details: { intangibles: detail("S2501R0070"), operationalRisk: detail("S2501R0130"), lacTp: detail("S2501R0140"), lacDt: detail("S2501R0150") },
    warnings,
  };
}
