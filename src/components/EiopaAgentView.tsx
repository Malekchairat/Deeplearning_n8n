"use client";

import { ArrowClockwise, CheckCircle, DownloadSimple, FileXls, Lightning, Plus, SpinnerGap, Trash, UploadSimple, Warning, WarningCircle, Wrench } from "@phosphor-icons/react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import type { Trace } from "@/components/AgentPipelineStrip";
import { importEiopaFromSheets, type ModuleKey, type Tier } from "@/lib/eiopaImport";
import { parseQrtWorkbook } from "@/lib/solvarisk";

type Row = { id: number; name: string; tier: Tier; amount: string; kind: string };
type Check = { name: string; rule: string; value: number; threshold: number; ok: boolean; note: string; kind: "floor" | "cap" };
type Eligibility = {
  available: number; eligible_scr: number; eligible_mcr: number; haircut: number; ratio_scr_pct: number; ratio_mcr_pct: number | null;
  t2_plus_t3_classified: number; checks: Check[];
};
type Source = { id: number; source: string; page: string | number; score: number; excerpt: string };
type ToolCall = { tool: string; args: Record<string, unknown>; result: string };
type AgentResult = {
  status: "COMPLETED" | "LLM_UNAVAILABLE"; answer: string | null; rule_confirmed: boolean | null; evidence_ids: number[]; kb_max_relevance: number;
  sources: Source[]; eligibility: Eligibility | null; file: { name: string; template: string; rows: number; sheets: string[]; computable: boolean; warnings: string[] } | null;
  tool_calls: ToolCall[]; model: string | null; errors: string[];
};

const TIERS: { value: Tier; label: string }[] = [
  { value: "T1_UNRESTRICTED", label: "Tier 1 non restreint" }, { value: "T1_RESTRICTED", label: "Tier 1 restreint" },
  { value: "T2", label: "Tier 2" }, { value: "T3", label: "Tier 3" },
];
const EXAMPLE_ROWS: Omit<Row, "id">[] = [
  { name: "Capital social et réserves", tier: "T1_UNRESTRICTED", amount: "228.5", kind: "capital" },
  { name: "Titres super-subordonnés perpétuels", tier: "T1_RESTRICTED", amount: "18", kind: "dette hybride" },
  { name: "Obligations subordonnées 2034", tier: "T2", amount: "95", kind: "dette subordonnée" },
  { name: "Actifs d'impôts différés nets", tier: "T3", amount: "42", kind: "DTA" },
];
type ModVals = Record<ModuleKey, { net: string; brut: string }>;
type Extra = { intang: string; op: string; lacTp: string; lacDt: string; mcrLinear: string; amcr: string };
const MODULE_DEFS: { key: ModuleKey; label: string }[] = [
  { key: "marche", label: "Marché" }, { key: "defaut", label: "Défaut de contrepartie" }, { key: "vie", label: "Souscription Vie" },
  { key: "sante", label: "Souscription Santé" }, { key: "nonvie", label: "Souscription Non-Vie" },
];
// the figures of qrt_pieuvre_scr/2_Mutuelle_IARD_Dominante.xlsx
const EXAMPLE_MODS: ModVals = {
  marche: { net: "108.73", brut: "115.26" }, defaut: { net: "33.21", brut: "33.21" }, vie: { net: "14.3", brut: "15.1" },
  sante: { net: "41.75", brut: "42.07" }, nonvie: { net: "163.11", brut: "163.11" },
};
const EMPTY_MODS: ModVals = { marche: { net: "", brut: "" }, defaut: { net: "", brut: "" }, vie: { net: "", brut: "" }, sante: { net: "", brut: "" }, nonvie: { net: "", brut: "" } };
const EXAMPLE_EXTRA: Extra = { intang: "1.8", op: "23.39", lacTp: "4.2", lacDt: "18.5", mcrLinear: "115", amcr: "4" };
const EMPTY_EXTRA: Extra = { intang: "", op: "", lacTp: "", lacDt: "", mcrLinear: "", amcr: "" };
const EXTRA_FIELDS: { key: keyof Extra; label: string }[] = [
  { key: "intang", label: "Actifs incorporels (R0070)" }, { key: "op", label: "Risque opérationnel (R0130)" },
  { key: "lacTp", label: "Absorption provisions techniques (R0140)" }, { key: "lacDt", label: "Absorption impôts différés (R0150)" },
];
const optionalNumber = (text: string) => (text.trim() === "" ? null : toNumber(text));
const EXAMPLE_QUESTION = "Vérifie si nos obligations subordonnées respectent les limites du Tier 2 selon la réglementation, et génère l'état S.23.01 des fonds propres au format Excel.";
const TOOL_LABEL: Record<string, string> = {
  search_eiopa_vector_db: "Recherche dans la base réglementaire", get_own_funds_portfolio: "Lecture du portefeuille",
  check_tier_limits: "Calcul des limites par Tier", generate_excel_qrt: "Génération de l'Excel QRT",
};
const CLIENT_TIMEOUT_MS = 150000;
const euro2 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const toNumber = (text: string) => Number(String(text).trim().replace(/\s/g, "").replace(",", "."));
const statusOf = (c: Check) => (c.ok ? "OK" : c.kind === "cap" ? "PLAFONNÉ" : "ÉCART");
const STATUS_STYLE: Record<string, string> = {
  OK: "border-emerald-200 bg-emerald-50 text-emerald-700", PLAFONNÉ: "border-amber-200 bg-amber-50 text-amber-800", ÉCART: "border-red-200 bg-red-50 text-red-700",
};

let nextRowId = 1;
const withIds = (rows: Omit<Row, "id">[]): Row[] => rows.map((r) => ({ ...r, id: nextRowId++ }));

export default function EiopaAgentView({ onTrace }: { onTrace: (agent: Trace["agent"], payload: string) => void }) {
  const [rows, setRows] = useState<Row[]>(() => withIds(EXAMPLE_ROWS));
  const [scr, setScr] = useState("251.17");
  const [mcr, setMcr] = useState("113.03");
  const [mods, setMods] = useState<ModVals>(EXAMPLE_MODS);
  const [extra, setExtra] = useState<Extra>(EXAMPLE_EXTRA);
  const [importNote, setImportNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [question, setQuestion] = useState(EXAMPLE_QUESTION);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<AgentResult | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!running) return;
    setElapsed(0);
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const updateRow = (id: number, patch: Partial<Row>) => setRows((current) => current.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const problems = (() => {
    const out: string[] = [];
    if (!(toNumber(scr) > 0)) out.push("Le SCR doit être un nombre positif.");
    if (!(toNumber(mcr) >= 0)) out.push("Le MCR doit être un nombre positif ou nul.");
    if (!rows.length) out.push("Ajoutez au moins un instrument.");
    if (rows.some((r) => !r.name.trim() || !(toNumber(r.amount) >= 0))) out.push("Chaque instrument a besoin d'un nom et d'un montant positif ou nul.");
    const filled = [...MODULE_DEFS.flatMap((d) => [mods[d.key].net, mods[d.key].brut]), ...Object.values(extra)].filter((v) => v.trim() !== "");
    if (filled.some((v) => !(toNumber(v) >= 0))) out.push("Les montants des modules et lignes S.25.01 / S.28.01 doivent être positifs ou nuls (laissez vide si inconnu).");
    if (question.trim().length < 3) out.push("Posez une question.");
    return out;
  })();

  // Fills the whole form from a QRT workbook such as qrt_pieuvre_scr/2_Mutuelle_IARD_Dominante.xlsx
  const importWorkbook = async (file?: File) => {
    if (!file) return;
    try {
      const imported = importEiopaFromSheets(parseQrtWorkbook(await file.arrayBuffer()));
      if (!imported.items.length && imported.scr === null) throw new Error(imported.warnings[0] ?? "Classeur QRT non reconnu.");
      setRows(withIds(imported.items.map((i) => ({ name: i.name, tier: i.tier, amount: String(i.amount), kind: "" }))));
      if (imported.scr !== null) setScr(String(imported.scr));
      if (imported.mcr !== null) setMcr(String(imported.mcr));
      setMods(Object.fromEntries(MODULE_DEFS.map((d) => [d.key, { net: String(imported.modules[d.key]?.net ?? ""), brut: String(imported.modules[d.key]?.brut ?? "") }])) as ModVals);
      const v = (n: number | null) => (n === null ? "" : String(n));
      setExtra({ intang: v(imported.details.intangibles), op: v(imported.details.operationalRisk), lacTp: v(imported.details.lacTp), lacDt: v(imported.details.lacDt), mcrLinear: v(imported.mcrLinear), amcr: v(imported.amcr) });
      setImportNote({ ok: !imported.warnings.length, text: `${file.name} : ${imported.items.length} instrument(s), ${Object.keys(imported.modules).length} module(s) de risque.${imported.warnings.length ? " " + imported.warnings.join(" ") : ""}` });
    } catch (e) {
      setImportNote({ ok: false, text: e instanceof Error ? e.message : "Impossible de lire ce classeur." });
    }
  };

  const run = async () => {
    if (problems.length || running) return;
    setError(""); setResult(null); setRunning(true);
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CLIENT_TIMEOUT_MS);
    try {
      const modules = MODULE_DEFS.filter((d) => mods[d.key].net.trim() !== "").map((d) => ({ name: d.label, value: toNumber(mods[d.key].net), ...(mods[d.key].brut.trim() !== "" ? { brut: toNumber(mods[d.key].brut) } : {}) }));
      const details = Object.fromEntries(([["intangibles", extra.intang], ["operationalRisk", extra.op], ["lacTp", extra.lacTp], ["lacDt", extra.lacDt]] as const)
        .filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, toNumber(v)]));
      const body = {
        question: question.trim(),
        own_funds: {
          scr: toNumber(scr), mcr: toNumber(mcr), mcr_linear: optionalNumber(extra.mcrLinear), amcr: optionalNumber(extra.amcr),
          items: rows.map((r) => ({ name: r.name.trim(), tier: r.tier, amount: toNumber(r.amount), kind: r.kind.trim() || null })),
        },
        // the S.25.01 lines make the generated workbook computable by the SCR agent
        ...(modules.length ? { solvency: { modules, details } } : {}),
      };
      const response = await fetch("/api/n8n/eiopa-agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.error === "string" ? data.error.replace(/^python: /, "") : `Erreur HTTP ${response.status}`);
      setResult(data as AgentResult);
      onTrace("RAG Agent", JSON.stringify({ eiopa: data.status, tools: data.tool_calls?.length ?? 0, rule_confirmed: data.rule_confirmed, latency: Math.round(performance.now() - started) }));
    } catch (e) {
      const raw = e instanceof DOMException && e.name === "AbortError" ? "L'agent a mis trop de temps à répondre (plus de 2 minutes). Réessayez." : e instanceof Error ? e.message : "Erreur inconnue.";
      const message = /HTTP 404|Not Found/i.test(raw) ? "Le service Python ne connaît pas encore l'agent EIOPA : redémarrez-le (dans rag_service : uvicorn main:app --port 8001) puis réessayez."
        : /fetch failed|ECONNREFUSED|injoignable/i.test(raw) ? "Le service Python est injoignable (port 8001). Démarrez-le (dans rag_service : uvicorn main:app --port 8001) puis réessayez."
          : raw;
      setError(message);
      onTrace("RAG Agent", JSON.stringify({ eiopa: "error", error: message }));
    } finally {
      clearTimeout(timer);
      setRunning(false);
    }
  };

  const ruleBadge = result && (result.rule_confirmed === true
    ? { text: "Règle confirmée par la base", cls: "border-emerald-200 bg-emerald-50 text-emerald-700", icon: CheckCircle }
    : result.rule_confirmed === false
      ? { text: "Règle non confirmée par la base", cls: "border-amber-200 bg-amber-50 text-amber-800", icon: Warning }
      : { text: "Confirmation non évaluée", cls: "border-line bg-paper text-stone", icon: Warning });

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,.95fr)_minmax(0,1.05fr)]">
      <div className="space-y-5">
        <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
          <div className="flex items-start justify-between gap-3">
            <div><span className="font-mono text-xs font-bold text-clay">EIOPA AGENT</span><h2 className="mt-2 text-3xl font-semibold tracking-tight">Fonds propres</h2></div>
            <div className="flex shrink-0 flex-wrap justify-end gap-2">
              <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-bold hover:bg-paper"><UploadSimple size={14} /> Importer un classeur QRT
                <input type="file" accept=".xlsx,.xls" className="hidden" onChange={(e) => { void importWorkbook(e.target.files?.[0]); e.currentTarget.value = ""; }} />
              </label>
              <button onClick={() => { setRows(withIds(EXAMPLE_ROWS)); setScr("251.17"); setMcr("113.03"); setMods(EXAMPLE_MODS); setExtra(EXAMPLE_EXTRA); setImportNote(null); setQuestion(EXAMPLE_QUESTION); }} className="rounded-lg border border-line px-3 py-2 text-xs font-bold hover:bg-paper">Charger l&apos;exemple</button>
            </div>
          </div>
          {importNote && <p role="status" className={`mt-4 rounded-lg border px-3 py-2 text-xs ${importNote.ok ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-amber-200 bg-amber-50 text-amber-800"}`}>{importNote.text}</p>}
          <p className="mt-5 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs leading-5 text-blue-800">
            Le SCR et le MCR ne sont pas saisis ici : ils seront calculés ultérieurement par le moteur SCR à partir des données QRT.
          </p>
          <p className="mt-6 font-mono text-xs font-bold text-stone">INSTRUMENTS</p>
          <div className="mt-2 space-y-2">
            {rows.map((row) => (
              <div key={row.id} className="grid grid-cols-[1fr_auto] gap-2 rounded-xl border border-line p-2.5 sm:grid-cols-[1.4fr_9.5rem_5.5rem_auto]">
                <input aria-label="Instrument" value={row.name} onChange={(e) => updateRow(row.id, { name: e.target.value })} placeholder="Instrument" className="col-span-2 rounded-lg bg-paper px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-clay sm:col-span-1" />
                <select aria-label="Tier" value={row.tier} onChange={(e) => updateRow(row.id, { tier: e.target.value as Tier })} className="rounded-lg bg-paper px-2 py-2 text-sm outline-none focus:ring-1 focus:ring-clay">{TIERS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}</select>
                <input aria-label="Montant en M€" inputMode="decimal" value={row.amount} onChange={(e) => updateRow(row.id, { amount: e.target.value })} className="rounded-lg bg-paper px-3 py-2 text-right font-mono text-sm outline-none focus:ring-1 focus:ring-clay" />
                <button aria-label="Supprimer l'instrument" onClick={() => setRows((current) => current.filter((r) => r.id !== row.id))} className="rounded-lg p-2 text-stone hover:bg-red-50 hover:text-red-600"><Trash size={17} /></button>
              </div>
            ))}
          </div>
          <button onClick={() => setRows((current) => [...current, { id: nextRowId++, name: "", tier: "T2", amount: "", kind: "" }])} className="mt-3 flex items-center gap-1.5 rounded-lg border border-dashed border-clay/50 px-3 py-2 text-xs font-bold text-clay hover:bg-blue-50"><Plus size={14} weight="bold" /> Ajouter un instrument</button>
        </section>

        <details open className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
          <summary className="cursor-pointer font-mono text-xs font-bold text-stone">DONNÉES POUR L&apos;AGENT SCR (S.25.01 / S.28.01) · optionnel</summary>
          <p className="mt-3 text-xs leading-5 text-stone">Avec ces lignes, le fichier Excel généré reprend les feuilles <strong>S2501_SCR</strong> et <strong>Fonds_Propres_Ratios</strong> des classeurs QRT de référence : l&apos;agent SCR (onglet QRT &amp; SCR) peut alors le charger et le calculer. Sans elles, seul l&apos;état des fonds propres est généré. Montants en M€, positifs ; laissez vide si inconnu.</p>
          <div className="mt-4 overflow-x-auto"><table className="w-full text-sm"><thead className="font-mono text-[10px] text-stone"><tr><th className="py-1 pr-3 text-left">MODULE DE RISQUE</th><th className="px-2 py-1 text-right">NET (C0030)</th><th className="px-2 py-1 text-right">BRUT (C0040)</th></tr></thead>
            <tbody>{MODULE_DEFS.map((d) => (
              <tr key={d.key} className="border-t border-line/60"><td className="py-1.5 pr-3">{d.label}</td>
                {(["net", "brut"] as const).map((col) => <td key={col} className="px-1 py-1"><input aria-label={`${d.label} ${col}`} inputMode="decimal" value={mods[d.key][col]} onChange={(e) => setMods((m) => ({ ...m, [d.key]: { ...m[d.key], [col]: e.target.value } }))} className="w-full min-w-[4.5rem] rounded-lg bg-paper px-2 py-1.5 text-right font-mono text-sm outline-none focus:ring-1 focus:ring-clay" /></td>)}
              </tr>))}</tbody></table></div>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
            {EXTRA_FIELDS.map((f) => <label key={f.key} className="block text-xs font-semibold text-stone">{f.label}<input inputMode="decimal" value={extra[f.key]} onChange={(e) => setExtra((x) => ({ ...x, [f.key]: e.target.value }))} className="mt-1 w-full rounded-lg bg-paper px-3 py-2 text-right font-mono text-sm text-ink outline-none focus:ring-1 focus:ring-clay" /></label>)}
          </div>
          <button onClick={() => { setMods(EMPTY_MODS); setExtra(EMPTY_EXTRA); }} className="mt-3 text-xs font-semibold text-stone underline hover:text-ink">Vider ces lignes</button>
        </details>

        <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
          <span className="font-mono text-xs font-bold text-stone">QUESTION</span>
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={4} className="mt-2 w-full resize-y rounded-xl bg-paper px-4 py-3 text-sm leading-6 outline-none focus:ring-1 focus:ring-clay" />
          {problems.length > 0 && <ul className="mt-2 space-y-1 text-xs text-red-600">{problems.map((p) => <li key={p}>• {p}</li>)}</ul>}
          <motion.button whileTap={{ scale: .97 }} onClick={run} disabled={running || problems.length > 0} className="mt-4 flex items-center gap-2 rounded-full bg-ink px-5 py-3 text-sm font-bold text-white shadow-sm hover:bg-clay disabled:cursor-not-allowed disabled:opacity-40">
            {running ? <SpinnerGap size={18} className="animate-spin" /> : <Lightning size={18} weight="fill" />}{running ? `L'agent travaille… ${elapsed}s` : "Lancer l'agent"}
          </motion.button>
          <p className="mt-3 text-xs leading-5 text-stone">L&apos;agent cherche la règle dans la base documentaire, lit ces données, calcule les limites par Tier puis génère l&apos;Excel. Les montants viennent de ce formulaire : le modèle n&apos;en saisit aucun. Compter 1 à 2 minutes.</p>
        </section>
      </div>

      <div className="space-y-5">
        {running && (
          <section role="status" aria-live="polite" className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
            <span className="font-mono text-xs font-bold text-clay">AGENT EN COURS</span>
            <ol className="mt-4 space-y-2 text-sm">{Object.values(TOOL_LABEL).map((label) => <li key={label} className="flex items-center gap-2 text-stone"><span className="h-1.5 w-1.5 rounded-full bg-clay/60" />{label}</li>)}</ol>
            <div className="mt-5 animate-pulse space-y-2"><div className="h-3 w-full rounded bg-paper" /><div className="h-3 w-5/6 rounded bg-paper" /><div className="h-3 w-2/3 rounded bg-paper" /></div>
          </section>
        )}
        {error && (
          <div role="alert" className="flex items-start justify-between gap-4 rounded-[20px] border border-red-200 bg-red-50 p-5 text-sm text-red-700">
            <div><p className="font-semibold">L&apos;agent n&apos;a pas pu terminer.</p><p className="mt-1 break-words text-xs text-red-600/80">{error.slice(0, 400)}</p></div>
            <button onClick={run} className="flex shrink-0 items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs font-bold hover:bg-red-100"><ArrowClockwise size={14} /> Réessayer</button>
          </div>
        )}
        {!running && !error && !result && (
          <div className="flex min-h-[320px] flex-col items-center justify-center rounded-[20px] border border-dashed border-line bg-white p-8 text-center shadow-tactile">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-clay"><FileXls size={27} weight="duotone" /></div>
            <h3 className="mt-4 text-lg font-bold">Expert EIOPA &amp; QRT</h3>
            <p className="mt-2 max-w-sm text-sm leading-6 text-stone">Renseignez les fonds propres puis lancez l&apos;agent : verdict sourcé, contrôles des limites par Tier et état S.23.01 en Excel.</p>
          </div>
        )}

        {result && (
          <>
            {result.status === "LLM_UNAVAILABLE" && (
              <div role="alert" className="rounded-[20px] border border-amber-200 bg-amber-50 p-5 text-sm text-amber-800">
                <p className="font-semibold">Le modèle n&apos;a pas répondu : seul le calcul déterministe est affiché (pas de recherche réglementaire ni de fichier).</p>
                {result.errors.slice(0, 2).map((e) => <p key={e} className="mt-1 break-words text-xs">{e.slice(0, 220)}</p>)}
                <button onClick={run} className="mt-3 flex items-center gap-1.5 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-bold hover:bg-amber-100"><ArrowClockwise size={14} /> Relancer</button>
              </div>
            )}
            {result.answer && ruleBadge && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div><span className="font-mono text-xs font-bold text-clay">VERDICT DE L&apos;AGENT</span>{result.model && <span className="ml-2 font-mono text-[10px] text-stone">{result.model}</span>}</div>
                  <span className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-bold ${ruleBadge.cls}`}><ruleBadge.icon size={14} weight="fill" />{ruleBadge.text}</span>
                </div>
                <p className="mt-4 whitespace-pre-wrap text-sm leading-7">{result.answer}</p>
                {result.rule_confirmed !== true && <p className="mt-4 rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">La base documentaire ne contient pas explicitement les limites utilisées (le calcul applique celles de la formule standard). Pour pouvoir les confirmer, ajoutez le Règlement délégué (UE) 2015/35 ou les guidelines EIOPA dans <code>rag_service/documents/</code> et redémarrez le service.</p>}
                {result.file && (
                  <a href={`/api/eiopa-file/${result.file.name}`} download className="mt-5 inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-bold text-white shadow-sm hover:bg-emerald-700"><DownloadSimple size={18} weight="bold" /> Télécharger {result.file.template} (Excel)</a>
                )}
                {result.file && <p className="mt-2 font-mono text-[10px] text-stone">{result.file.name} · {result.file.rows} lignes · feuilles : {result.file.sheets.join(", ")}</p>}
                {result.file && (result.file.computable
                  ? <p className="mt-3 flex items-start gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs leading-5 text-emerald-800"><CheckCircle size={15} weight="fill" className="mt-0.5 shrink-0" /><span><strong>Calculable par l&apos;agent SCR.</strong> Importez ce fichier dans l&apos;onglet « QRT &amp; SCR » puis cliquez sur Calculer.{result.file.warnings.length > 0 && <> {result.file.warnings.join(" ")}</>}</span></p>
                  : <p className="mt-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800"><Warning size={15} weight="fill" className="mt-0.5 shrink-0" /><span><strong>Non calculable par l&apos;agent SCR.</strong> {result.file.warnings.join(" ")} Renseignez la section « Données pour l&apos;agent SCR » puis relancez.</span></p>)}
              </section>
            )}
            {result.eligibility && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <span className="font-mono text-xs font-bold text-stone">FONDS PROPRES ÉLIGIBLES (CALCULÉS PAR L&apos;OUTIL)</span>
                <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {([["Disponibles", `${euro2.format(result.eligibility.available)} M€`], ["Éligibles SCR", `${euro2.format(result.eligibility.eligible_scr)} M€`], ["Ratio SCR", `${euro2.format(result.eligibility.ratio_scr_pct)} %`],
                    ["Ratio MCR", result.eligibility.ratio_mcr_pct === null ? "n/a" : `${euro2.format(result.eligibility.ratio_mcr_pct)} %`]] as const).map(([label, value]) => (
                    <div key={label} className="rounded-xl bg-paper p-3"><p className="text-xs text-stone">{label}</p><p className="mt-1 font-mono text-base font-bold">{value}</p></div>
                  ))}
                </div>
                <div className="mt-5 overflow-x-auto"><table className="w-full text-left text-sm"><thead className="font-mono text-[10px] text-stone"><tr><th className="py-2 pr-3">CONTRÔLE</th><th className="px-2 py-2 text-right">VALEUR</th><th className="px-2 py-2 text-right">SEUIL</th><th className="py-2 pl-3">STATUT</th></tr></thead>
                  <tbody>{result.eligibility.checks.map((c) => { const status = statusOf(c); return (
                    <tr key={c.name} className="border-t border-line/70 align-top"><td className="py-2.5 pr-3">{c.name}{c.note && <p className="mt-0.5 text-xs text-stone">{c.note}</p>}</td><td className="px-2 py-2.5 text-right font-mono">{euro2.format(c.value)}</td><td className="px-2 py-2.5 text-right font-mono text-stone">{euro2.format(c.threshold)}</td>
                      <td className="py-2.5 pl-3"><span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${STATUS_STYLE[status]}`}>{status}</span></td></tr>); })}</tbody></table></div>
                <p className="mt-3 text-xs leading-5 text-stone"><strong>PLAFONNÉ</strong> : plafond d&apos;éligibilité dépassé, l&apos;excédent n&apos;est pas éligible (pas une infraction). <strong>ÉCART</strong> : minimum non atteint. Les critères qualitatifs de classement (subordination, durée…) ne sont pas vérifiés.</p>
              </section>
            )}
            {result.sources.length > 0 && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <div className="flex items-center justify-between"><span className="font-mono text-xs font-bold text-stone">SOURCES RETROUVÉES</span><span className="font-mono text-[10px] text-stone">pertinence max {euro2.format(result.kb_max_relevance)}</span></div>
                <ul className="mt-4 space-y-3">{result.sources.map((s) => (
                  <li key={s.id} className="text-sm"><div className="flex items-center justify-between gap-2"><strong className="min-w-0 truncate">[{s.id}] {s.source} <span className="font-normal text-stone">p. {s.page}</span></strong>{result.evidence_ids.includes(s.id) && <span className="shrink-0 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-bold text-emerald-700">preuve</span>}</div>
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-paper"><div className="h-full rounded-full bg-clay/70" style={{ width: `${Math.min(100, s.score * 100)}%` }} /></div>
                    <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-stone">{s.excerpt}</p></li>))}</ul>
              </section>
            )}
            {result.tool_calls.length > 0 && (
              <details className="rounded-[20px] border border-line bg-white p-5 shadow-tactile">
                <summary className="flex cursor-pointer items-center gap-2 font-mono text-xs font-bold text-stone"><Wrench size={15} /> ENCHAÎNEMENT DES OUTILS ({result.tool_calls.length})</summary>
                <ol className="mt-4 space-y-3">{result.tool_calls.map((c, i) => (
                  <li key={`${c.tool}-${i}`} className="text-sm"><p className="font-semibold">{i + 1}. {TOOL_LABEL[c.tool] ?? c.tool} <code className="font-mono text-[11px] font-normal text-stone">{c.tool}</code></p>
                    {Object.keys(c.args).length > 0 && <p className="mt-0.5 break-words font-mono text-[11px] text-clay">{JSON.stringify(c.args).slice(0, 160)}</p>}
                    <p className="mt-0.5 line-clamp-3 break-words text-xs text-stone">{c.result.slice(0, 240)}</p></li>))}</ol>
              </details>
            )}
            {result.status === "COMPLETED" && result.errors.length > 0 && <p className="flex items-start gap-2 rounded-lg bg-paper px-3 py-2 text-xs text-stone"><WarningCircle size={15} className="mt-0.5 shrink-0" /> Le modèle principal a échoué, le modèle de secours a répondu : {result.errors[0].slice(0, 160)}</p>}
          </>
        )}
      </div>
    </div>
  );
}
