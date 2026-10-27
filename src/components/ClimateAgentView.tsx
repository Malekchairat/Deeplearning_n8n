"use client";

import { CloudRain, DownloadSimple, Lightning, SpinnerGap, UploadSimple, Warning } from "@phosphor-icons/react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import type { Trace } from "@/components/AgentPipelineStrip";
import { CLIMATE_TEMPLATE_CSV, parseClimatePortfolio, type ClimateProperty } from "@/lib/climateImport";

type ClassRow = { hazard: string; label: string; zones: number; sum_insured_meur: number; share_pct: number };
type ZoneRow = { zone: string; cities: string[]; hazard: string; label: string; sum_insured_meur: number; annual_event_prob: number | null; horizon_event_prob: number | null; rx1_mm: number | null; river_discharge_ratio: number | null };
type Stress = {
  horizon_years: number; scr_nonlife_meur: number; covered_sum_insured_meur: number; excluded_no_cover_meur: number; unassessed_sum_insured_meur: number;
  by_class: ClassRow[]; threatened_pct_of_scr: number; threatened_scr_meur: number; high_zone_pct: number; elevated_or_moderate_pct: number;
  scenario_loss_meur: number; scenario_loss_pct_of_scr: number; damage_ratio: number; zones: ZoneRow[]; method: string;
};
type ToolCall = { tool: string; args: Record<string, unknown>; result: string };
type ClimateResult = { portfolio_source?: "demo" | "client"; status: "COMPLETED" | "LLM_UNAVAILABLE" | "DATA_UNAVAILABLE"; answer: string | null; stress: Stress | null; zones_fetched: number; tool_calls: ToolCall[]; model: string | null; key_source: string | null; errors: string[] };

const EXAMPLE_QUESTION = "Analyse notre portefeuille d'assurance habitation. Croise les localisations avec l'API climatique et dis-moi quel pourcentage du SCR Non-Vie est menacé par le risque inondation d'ici 5 ans.";
const TOOL_LABEL = ["Lecture de la base du portefeuille", "Interrogation de l'API climatique par zone", "Calcul du stress test inondation"];
const TOOL_NAME: Record<string, string> = { query_portfolio_database: "Base du portefeuille", fetch_geospatial_weather_api: "API climatique", compute_climate_stress: "Calcul du stress test" };
const HAZARD_CLS: Record<string, string> = { HIGH: "border-red-200 bg-red-50 text-red-700", MEDIUM: "border-amber-200 bg-amber-50 text-amber-800", LOW: "border-emerald-200 bg-emerald-50 text-emerald-700", UNKNOWN: "border-line bg-paper text-stone" };
const CLIENT_TIMEOUT_MS = 150000;
const fr = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const toNumber = (text: string) => Number(text.replace(",", ".").trim());

export default function ClimateAgentView({ onTrace }: { onTrace: (agent: Trace["agent"], payload: string) => void }) {
  const [scr, setScr] = useState("163.11");
  const [horizon, setHorizon] = useState("5");
  const [question, setQuestion] = useState(EXAMPLE_QUESTION);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<ClimateResult | null>(null);
  const [error, setError] = useState("");
  const [book, setBook] = useState<{ name: string; properties: ClimateProperty[] } | null>(null);
  const [bookNote, setBookNote] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!running) return;
    setElapsed(0);
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const problems = [
    ...(!(toNumber(scr) > 0) ? ["Le SCR Non-Vie doit être un nombre positif (M€)."] : []),
    ...(!(Number.isInteger(toNumber(horizon)) && toNumber(horizon) >= 1 && toNumber(horizon) <= 25) ? ["L'horizon doit être un nombre entier d'années entre 1 et 25."] : []),
    ...(question.trim().length < 3 ? ["Posez une question."] : []),
  ];

  const importBook = async (file?: File) => {
    if (!file) return;
    try {
      const { properties, skipped } = parseClimatePortfolio(await file.arrayBuffer());
      setBook({ name: file.name, properties });
      setBookNote({ ok: true, text: `${properties.length} lignes importées${skipped ? `, ${skipped} ignorées (champs manquants ou invalides)` : ""}.` });
      setResult(null);
    } catch (e) {
      setBookNote({ ok: false, text: e instanceof Error ? e.message : "Import impossible." });
    }
  };

  const downloadTemplate = () => {
    const url = URL.createObjectURL(new Blob(["﻿" + CLIMATE_TEMPLATE_CSV], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a"); a.href = url; a.download = "portefeuille_habitation_modele.csv"; a.click(); URL.revokeObjectURL(url);
  };

  const run = async () => {
    if (problems.length || running) return;
    setError(""); setResult(null); setRunning(true);
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CLIENT_TIMEOUT_MS);
    try {
      const response = await fetch("/api/n8n/climate-agent", { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ question: question.trim(), scr_nonlife: toNumber(scr), horizon_years: toNumber(horizon), ...(book ? { properties: book.properties } : {}) }) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.error === "string" ? data.error.replace(/^python: /, "") : `Erreur HTTP ${response.status}`);
      setResult(data as ClimateResult);
      onTrace("RAG Agent", JSON.stringify({ climate: data.status, zones: data.zones_fetched, threatened_pct: data.stress?.threatened_pct_of_scr, latency: Math.round(performance.now() - started) }));
    } catch (e) {
      const raw = e instanceof DOMException && e.name === "AbortError" ? "L'agent a mis trop de temps à répondre (plus de 2 minutes). Réessayez." : e instanceof Error ? e.message : "Erreur inconnue.";
      const message = /HTTP 404|Not Found/i.test(raw) ? "Le service Python ne connaît pas encore l'agent climat : redémarrez-le (dans rag_service : uvicorn main:app --port 8001) puis réessayez."
        : /fetch failed|ECONNREFUSED|injoignable/i.test(raw) ? "Le service Python est injoignable (port 8001). Démarrez-le (dans rag_service : uvicorn main:app --port 8001) puis réessayez." : raw;
      setError(message);
      onTrace("RAG Agent", JSON.stringify({ climate: "error", error: message }));
    } finally {
      clearTimeout(timer);
      setRunning(false);
    }
  };

  const s = result?.stress;
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,.8fr)_minmax(0,1.2fr)]">
      <div className="space-y-5">
        <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
          <span className="font-mono text-xs font-bold text-clay">CLIMATE & ESG AGENT</span>
          <h2 className="mt-2 text-3xl font-semibold tracking-tight">Stress test climatique</h2>
          <p className="mt-2 text-xs leading-5 text-stone">Test de résistance ORSA (Pilier 2) : l&apos;agent croise la base des biens assurés (habitation, Non-Vie) avec l&apos;API climatique Open-Meteo et mesure la part du SCR Non-Vie exposée aux inondations.</p>
          <div className="mt-5 grid grid-cols-2 gap-3">
            <label className="block text-xs font-semibold text-stone">SCR Non-Vie (M€)<input inputMode="decimal" value={scr} onChange={(e) => setScr(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-paper px-3 py-2 font-mono text-sm text-ink outline-none focus:border-clay" /></label>
            <label className="block text-xs font-semibold text-stone">Horizon (années)<input inputMode="numeric" value={horizon} onChange={(e) => setHorizon(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-paper px-3 py-2 font-mono text-sm text-ink outline-none focus:border-clay" /></label>
          </div>
          <p className="mt-6 font-mono text-xs font-bold text-stone">PORTEFEUILLE</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-bold hover:bg-paper"><UploadSimple size={14} /> Importer mon portefeuille (CSV / Excel)
              <input type="file" accept=".csv,.xlsx,.xls" className="hidden" onChange={(e) => { void importBook(e.target.files?.[0]); e.currentTarget.value = ""; }} /></label>
            <button onClick={downloadTemplate} className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-bold hover:bg-paper"><DownloadSimple size={14} /> Modèle CSV</button>
            {book && <button onClick={() => { setBook(null); setBookNote(null); setResult(null); }} className="text-xs font-semibold text-stone underline hover:text-ink">Revenir à la démo</button>}
          </div>
          <p className="mt-2 text-xs text-stone">Source : <strong>{book ? `${book.name} (${book.properties.length} lignes)` : "portefeuille de démonstration (fixe)"}</strong></p>
          {bookNote && <p role="status" className={`mt-2 rounded-lg border px-3 py-2 text-xs ${bookNote.ok ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-amber-200 bg-amber-50 text-amber-800"}`}>{bookNote.text}</p>}
          <p className="mt-6 font-mono text-xs font-bold text-stone">QUESTION</p>
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={5} className="mt-2 w-full resize-y rounded-xl bg-paper px-4 py-3 text-sm leading-6 outline-none focus:ring-1 focus:ring-clay" />
          {problems.length > 0 && <ul className="mt-2 space-y-1 text-xs text-red-600">{problems.map((p) => <li key={p}>• {p}</li>)}</ul>}
          <motion.button whileTap={{ scale: .97 }} onClick={run} disabled={running || problems.length > 0} className="mt-4 flex items-center gap-2 rounded-full bg-ink px-5 py-3 text-sm font-bold text-white shadow-sm hover:bg-clay disabled:cursor-not-allowed disabled:opacity-40">
            {running ? <SpinnerGap size={18} className="animate-spin" /> : <Lightning size={18} weight="fill" />}{running ? `L'agent travaille… ${elapsed}s` : "Lancer le stress test"}
          </motion.button>
          <p className="mt-3 text-xs leading-5 text-stone">Sans import, l&apos;agent lit un portefeuille de démonstration fixe (34 lots, 15 villes) : le % menacé change avec l&apos;horizon et le portefeuille importé, mais pas avec le SCR (qui ne change que les montants en M€). Les calculs sont faits par le code, pas par le modèle. Compter 30 secondes à 2 minutes.</p>
        </section>
      </div>

      <div className="space-y-5">
        {running && (
          <section role="status" aria-live="polite" className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
            <span className="font-mono text-xs font-bold text-clay">AGENT EN COURS</span>
            <ol className="mt-4 space-y-2 text-sm">{TOOL_LABEL.map((label) => <li key={label} className="flex items-center gap-2 text-stone"><span className="h-1.5 w-1.5 rounded-full bg-clay/60" />{label}</li>)}</ol>
            <div className="mt-5 animate-pulse space-y-2"><div className="h-3 w-full rounded bg-paper" /><div className="h-3 w-5/6 rounded bg-paper" /><div className="h-3 w-2/3 rounded bg-paper" /></div>
          </section>
        )}
        {error && <p role="alert" className="flex items-start gap-2 rounded-[20px] border border-red-200 bg-red-50 p-5 text-sm text-red-700"><Warning size={18} className="mt-0.5 shrink-0" />{error}</p>}
        {!running && !error && !result && (
          <section className="rounded-[20px] border border-dashed border-line bg-white/60 p-10 text-center text-sm text-stone"><CloudRain size={32} className="mx-auto mb-3 text-clay" />Lancez le stress test : le rapport, les expositions par zone et la part du SCR Non-Vie menacée apparaîtront ici.</section>
        )}
        {result && (
          <>
            {result.status === "LLM_UNAVAILABLE" && <p role="status" className="rounded-[20px] border border-amber-200 bg-amber-50 p-5 text-sm text-amber-800"><strong>Le modèle n&apos;a pas répondu : seul le calcul déterministe est affiché</strong> (mêmes données climatiques, mêmes formules).{result.errors.length > 0 && <span className="mt-1 block break-words font-mono text-[11px]">{result.errors.join(" · ")}</span>}</p>}
            {result.status === "DATA_UNAVAILABLE" && <p role="alert" className="rounded-[20px] border border-red-200 bg-red-50 p-5 text-sm text-red-700"><strong>API climatique indisponible</strong> : aucune zone n&apos;a pu être évaluée, aucun résultat n&apos;est affiché plutôt qu&apos;un résultat inventé. Réessayez plus tard.</p>}
            {s && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <span className="font-mono text-xs font-bold text-stone">SCR NON-VIE MENACÉ PAR L&apos;INONDATION · HORIZON {s.horizon_years} ANS</span>
                <div className="mt-3 flex flex-wrap items-end gap-x-8 gap-y-3">
                  <div><p className="font-mono text-5xl font-semibold tracking-tight text-clay">{fr.format(s.threatened_pct_of_scr)} %</p><p className="mt-1 text-xs text-stone">soit {fr.format(s.threatened_scr_meur)} M€ sur {fr.format(s.scr_nonlife_meur)} M€ (pondéré par la probabilité d&apos;épisode sur {s.horizon_years} an{s.horizon_years > 1 ? "s" : ""})</p></div>
                  <div><p className="font-mono text-2xl font-semibold">{fr.format(s.high_zone_pct)} %</p><p className="mt-1 text-xs text-stone">de la valeur en zones à risque élevé</p></div>
                  <div><p className="font-mono text-2xl font-semibold">{fr.format(s.scenario_loss_meur)} M€</p><p className="mt-1 text-xs text-stone">perte attendue = {fr.format(s.scenario_loss_pct_of_scr)} % du SCR</p></div>
                </div>
                <table className="mt-5 w-full text-sm"><thead className="font-mono text-[10px] text-stone"><tr><th className="py-1 text-left">RISQUE INONDATION</th><th className="py-1 text-right">ZONES</th><th className="py-1 text-right">VALEUR ASSURÉE (M€)</th><th className="py-1 text-right">PART</th></tr></thead>
                  <tbody>{s.by_class.map((c) => <tr key={c.hazard} className="border-t border-line/60"><td className="py-1.5"><span className={`rounded-full border px-2 py-0.5 text-xs font-bold ${HAZARD_CLS[c.hazard]}`}>{c.label}</span></td><td className="py-1.5 text-right font-mono">{c.zones}</td><td className="py-1.5 text-right font-mono">{fr.format(c.sum_insured_meur)}</td><td className="py-1.5 text-right font-mono">{fr.format(c.share_pct)} %</td></tr>)}</tbody></table>
                {s.unassessed_sum_insured_meur > 0 && <p className="mt-3 text-xs text-amber-800">{fr.format(s.unassessed_sum_insured_meur)} M€ de valeur assurée n&apos;ont pas pu être évalués (API indisponible pour ces zones) : ils ne sont pas comptés comme sûrs.</p>}
                <p className="mt-3 text-xs text-stone">Hors périmètre : {fr.format(s.excluded_no_cover_meur)} M€ de contrats sans garantie inondation.</p>
              </section>
            )}
            {result.answer && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <div className="flex items-center justify-between gap-3"><span className="font-mono text-xs font-bold text-clay">RAPPORT DE L&apos;AGENT</span>{result.model && <span className="font-mono text-[10px] text-stone">{result.model}</span>}</div>
                <p className="mt-3 whitespace-pre-line text-sm leading-6">{result.answer}</p>
              </section>
            )}
            {s && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <span className="font-mono text-xs font-bold text-stone">EXPOSITION PAR ZONE ({s.zones.length})</span>
                <div className="mt-3 overflow-x-auto"><table className="w-full text-sm"><thead className="font-mono text-[10px] text-stone"><tr><th className="py-1 text-left">ZONE</th><th className="px-2 py-1 text-left">RISQUE</th><th className="px-2 py-1 text-right">M€ ASSURÉS</th><th className="px-2 py-1 text-right">PROBA. ÉPISODE</th><th className="px-2 py-1 text-right">DÉBIT RIV.</th></tr></thead>
                  <tbody>{s.zones.map((z) => <tr key={z.zone} className="border-t border-line/60"><td className="py-1.5">{z.cities.join(", ")}</td><td className="px-2 py-1.5"><span className={`rounded-full border px-2 py-0.5 text-xs font-bold ${HAZARD_CLS[z.hazard]}`}>{z.label}</span></td><td className="px-2 py-1.5 text-right font-mono">{fr.format(z.sum_insured_meur)}</td><td className="px-2 py-1.5 text-right font-mono">{z.horizon_event_prob == null ? "—" : `${Math.round(z.horizon_event_prob * 100)} %`}</td><td className="px-2 py-1.5 text-right font-mono">{z.river_discharge_ratio ?? "—"}</td></tr>)}</tbody></table></div>
                <details className="mt-4 text-xs leading-5 text-stone"><summary className="cursor-pointer font-semibold">Méthode et limites</summary>
                  <p className="mt-2">{s.method}</p>
                  <p className="mt-1">Taux de dommage supposé : {Math.round(s.damage_ratio * 1000) / 10} %. « Proba. épisode » = probabilité d&apos;au moins un jour de pluie ≥ 40 mm sur l&apos;horizon. Données : Open-Meteo (modèle climatique EC_Earth3P_HR et débits GloFAS), un seul modèle ; ce n&apos;est pas un modèle de catastrophe naturelle calibré.</p></details>
              </section>
            )}
            {result.tool_calls.length > 0 && (
              <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
                <span className="font-mono text-xs font-bold text-stone">TRACE DES OUTILS</span>
                <ol className="mt-3 space-y-1.5 text-sm">{result.tool_calls.map((t, i) => <li key={i} className="flex items-center gap-2"><span className="font-mono text-xs text-stone">{i + 1}.</span>{TOOL_NAME[t.tool] ?? t.tool}</li>)}</ol>
                {result.key_source && <p className="mt-3 font-mono text-[10px] text-stone">clé utilisée : {result.key_source}</p>}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
