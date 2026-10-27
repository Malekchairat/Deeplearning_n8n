"use client";

import { ArrowClockwise, ArrowDown, CheckCircle, FilePdf, FileArrowUp, Lightbulb, Lightning, MagnifyingGlass, SpinnerGap, UploadSimple, Warning, WarningCircle } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { assertValidScrResult, buildScrPayload, downloadOutput, parseQrtWorkbook, qrtRowsFromSheets, type QrtRow, type QrtSheets, type ScrResult } from "@/lib/solvarisk";

const euro = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const euro2 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); // same precision as the n8n engine output

type Severity = "high" | "medium" | "low";
type Finding = { indicator: string; evidence: string; interpretation: string };
type Driver = { driver: string; weightPct: number | null; comment: string };
type Mitigation = { lever: string; description: string; expectedEffect: string };
type Analysis = {
  status: string; severity: Severity; summary: string; findings: Finding[]; recommendedAction: string;
  // richer fields of the LangGraph analyst (empty with the n8n risk-agent)
  robustness: string; drivers: Driver[]; orsa: string[]; mitigations: Mitigation[]; model: string; notes: string[];
};

// Data Quality & Compliance agent (Python, rule based): mirrors rag_service/pipeline/schemas.py
type QualityAnomaly = { code: string; criterion: string; severity: "CRITICAL" | "MEDIUM" | "LOW"; message: string; field: string | null; row: number | null; record_id: string | null };
type QualityReport = {
  quality_score: number; status: "PASSED" | "WARNING" | "BLOCKED"; is_eligible_for_scr: boolean; rows_audited: number;
  anomalies: QualityAnomaly[]; anomalies_truncated: number; counts_by_severity: Record<string, number>; counts_by_code: Record<string, number>;
};
type QualityState = { phase: "idle" } | { phase: "loading" } | { phase: "done"; report: QualityReport } | { phase: "error"; message: string };
type AnalysisState = { phase: "idle" } | { phase: "loading" } | { phase: "done"; data: Analysis } | { phase: "error"; message: string };

type ReportState = { phase: "idle" } | { phase: "loading" } | { phase: "done"; url: string } | { phase: "error"; message: string };
type Toast = { id: number; emoji: string; title: string; text: string; tone: "success" | "error" };

const ANALYST_TIMEOUT_MS = 115000;   // pipeline: Gemini retries + fallback model can legitimately take > 1 min (proxy allows 120 s)
const REPORT_TIMEOUT_MS = 70000;
const ANALYST_ERROR = "L'analyste n'a pas pu générer le diagnostic.";
const TOAST_MS = 5500;

const failureMessage = (error: unknown, timeoutText: string) =>
  error instanceof DOMException && error.name === "AbortError" ? timeoutText : error instanceof Error ? error.message : "Erreur inconnue.";

function ToastStack({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div aria-live="polite" className="pointer-events-none fixed right-5 top-5 z-50 flex w-[min(92vw,22rem)] flex-col gap-3">
      <AnimatePresence initial={false}>
        {toasts.map((toast) => (
          <motion.div key={toast.id} layout initial={{ opacity: 0, x: 48, scale: .94 }} animate={{ opacity: 1, x: 0, scale: 1 }} exit={{ opacity: 0, x: 48, scale: .94 }} transition={{ type: "spring", stiffness: 380, damping: 28 }}
            className={`pointer-events-auto flex items-start gap-3 rounded-2xl border bg-white p-4 shadow-lg ${toast.tone === "error" ? "border-red-200" : "border-emerald-200"}`}>
            <motion.span initial={{ rotate: -14, scale: .6 }} animate={{ rotate: 0, scale: 1 }} transition={{ type: "spring", stiffness: 500, damping: 12, delay: .08 }} className="text-2xl leading-none">{toast.emoji}</motion.span>
            <div className="min-w-0 flex-1"><p className="text-sm font-bold">{toast.title}</p><p className="mt-0.5 text-xs leading-5 text-stone">{toast.text}</p></div>
            <button onClick={() => onDismiss(toast.id)} aria-label="Fermer la notification" className="text-lg leading-none text-stone hover:text-ink">×</button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

const severityOf = (value: unknown): Severity => {
  const s = String(value ?? "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  if (/(high|critic|elev|haut|severe)/.test(s)) return "high";
  if (/(med|moder|moyen)/.test(s)) return "medium";
  return "low";
};

// The webhook may answer { analysis: {...} }, the object itself, an array of those, or an LLM string wrapping JSON.
function parseAnalysis(raw: unknown, depth = 0): Analysis | null {
  if (depth > 4 || raw === null || raw === undefined) return null;
  if (Array.isArray(raw)) return parseAnalysis(raw[0], depth + 1);
  if (typeof raw === "string") {
    const text = raw.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
    try { return parseAnalysis(JSON.parse(text), depth + 1); } catch { return null; }
  }
  if (typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  // `diagnosis` = the LangGraph pipeline response ({ diagnosis, model, guardrail_notes, ... })
  for (const key of ["analysis", "diagnosis", "json", "output", "text"]) {
    if (obj[key] !== undefined && typeof obj.summary !== "string") {
      const nested = parseAnalysis(obj[key], depth + 1);
      if (nested) return { ...nested, model: nested.model || String(obj.model ?? ""), notes: nested.notes.length ? nested.notes : strings(obj.guardrail_notes) };
    }
  }
  if (typeof obj.summary !== "string" || !obj.summary.trim()) return null;
  const records = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is Record<string, unknown> => typeof v === "object" && v !== null) : []);
  const findings = records(obj.findings).map((f) => ({
    indicator: String(f.indicator ?? ""), evidence: String(f.evidence ?? ""), interpretation: String(f.interpretation ?? ""),
  })).filter((f) => f.indicator || f.interpretation);
  return {
    status: String(obj.status ?? ""), severity: severityOf(obj.severity), summary: obj.summary.trim(), findings, recommendedAction: String(obj.recommended_action ?? obj.recommendedAction ?? "").trim(),
    robustness: String(obj.robustness_assessment ?? "").trim(),
    drivers: records(obj.risk_drivers).map((d) => ({ driver: String(d.driver ?? ""), weightPct: typeof d.weight_pct === "number" ? d.weight_pct : null, comment: String(d.comment ?? "") })).filter((d) => d.driver),
    orsa: strings(obj.orsa_recommendations),
    mitigations: records(obj.mitigation_plans).map((m) => ({ lever: String(m.lever ?? ""), description: String(m.description ?? ""), expectedEffect: String(m.expected_effect ?? "") })).filter((m) => m.lever),
    model: "", notes: [],
  };
}

const strings = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.trim().length > 0) : []);

const SEVERITY_UI: Record<Severity, { label: string; badge: string; icon: typeof Warning; iconColor: string }> = {
  high: { label: "Critique", badge: "border-red-200 bg-red-50 text-red-700", icon: WarningCircle, iconColor: "text-red-600" },
  medium: { label: "Modérée", badge: "border-orange-200 bg-orange-50 text-orange-700", icon: Warning, iconColor: "text-orange-500" },
  low: { label: "Normale", badge: "border-emerald-200 bg-emerald-50 text-emerald-700", icon: CheckCircle, iconColor: "text-emerald-600" },
};

function AnalystSkeleton() {
  return (
    <div role="status" aria-live="polite" aria-label="L'analyste prépare son diagnostic" className="mt-5 animate-pulse space-y-5">
      <div className="space-y-2.5"><div className="h-4 w-full rounded bg-paper" /><div className="h-4 w-11/12 rounded bg-paper" /><div className="h-4 w-2/3 rounded bg-paper" /></div>
      <div className="space-y-3">{[0, 1, 2].map((i) => <div key={i} className="flex gap-3"><div className="h-6 w-6 shrink-0 rounded-full bg-paper" /><div className="flex-1 space-y-2"><div className="flex gap-2"><div className="h-4 w-28 rounded bg-paper" /><div className="h-4 w-14 rounded-full bg-paper" /></div><div className="h-3.5 w-5/6 rounded bg-paper" /></div></div>)}</div>
      <div className="h-16 rounded-xl bg-blue-50" />
      <p className="font-mono text-xs text-stone">L'analyste examine les indicateurs…</p>
    </div>
  );
}

const QUALITY_UI = {
  PASSED: { label: "Conforme", badge: "border-emerald-200 bg-emerald-50 text-emerald-700", bar: "bg-emerald-500" },
  WARNING: { label: "Avertissements", badge: "border-orange-200 bg-orange-50 text-orange-700", bar: "bg-orange-400" },
  BLOCKED: { label: "Bloqué", badge: "border-red-200 bg-red-50 text-red-700", bar: "bg-red-500" },
} as const;
const ANOMALY_DOT = { CRITICAL: "bg-red-500", MEDIUM: "bg-orange-400", LOW: "bg-slate-300" } as const;

// Step 0, before any calculation: the data quality gate. A BLOCKED audit stops the pipeline.
function QualityPanel({ state }: { state: QualityState }) {
  if (state.phase === "idle") return null;
  const ui = state.phase === "done" ? QUALITY_UI[state.report.status] : null;
  const report = state.phase === "done" ? state.report : null;
  return (
    <motion.section initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .3 }} className={`rounded-[20px] border bg-white p-5 shadow-tactile ${report?.status === "BLOCKED" ? "border-red-300" : "border-line"}`}>
      <div className="flex items-start justify-between gap-3">
        <div><span className="font-mono text-xs font-bold text-clay">DATA QUALITY AGENT</span><h3 className="mt-1.5 text-xl font-semibold tracking-tight">Contrôle qualité des données</h3></div>
        {ui && <span className={`shrink-0 rounded-full border px-3 py-1 text-xs font-bold ${ui.badge}`}>{ui.label}</span>}
      </div>
      {state.phase === "loading" && <p role="status" className="mt-3 flex animate-pulse items-center gap-2 text-sm text-stone"><SpinnerGap size={16} className="animate-spin" /> Audit des données avant calcul…</p>}
      {state.phase === "error" && <p role="alert" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">Audit qualité indisponible ({state.message}). Le calcul continue sans contrôle préalable.</p>}
      {report && ui && (
        <>
          <div className="mt-4 flex items-center gap-4">
            <p className="font-mono text-3xl font-bold">{euro.format(report.quality_score)}<span className="text-base text-stone"> / 100</span></p>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-paper"><motion.div initial={{ width: 0 }} animate={{ width: `${report.quality_score}%` }} transition={{ duration: .6 }} className={`h-full rounded-full ${ui.bar}`} /></div>
          </div>
          <p className="mt-2 text-xs text-stone">{report.rows_audited} lignes auditées · {report.counts_by_severity.CRITICAL ?? 0} critique(s), {report.counts_by_severity.MEDIUM ?? 0} moyenne(s), {report.counts_by_severity.LOW ?? 0} mineure(s)</p>
          {report.status === "BLOCKED" && <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm font-semibold text-red-700">Calcul du SCR suspendu : corrigez les anomalies critiques puis relancez.</p>}
          {report.anomalies.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {report.anomalies.slice(0, 6).map((a, i) => (
                <li key={`${a.code}-${a.row}-${i}`} className="flex items-start gap-2 text-sm">
                  <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ANOMALY_DOT[a.severity]}`} />
                  <span className="min-w-0"><strong className="font-mono text-xs">{a.code}</strong> <span className="text-stone">{a.message}</span>{(a.record_id || a.row) && <span className="ml-1 font-mono text-xs text-clay">[{a.record_id ?? `ligne ${a.row}`}]</span>}</span>
                </li>
              ))}
              {report.anomalies.length + report.anomalies_truncated > 6 && <li className="pl-4 text-xs text-stone">+ {report.anomalies.length + report.anomalies_truncated - 6} autre(s) anomalie(s)</li>}
            </ul>
          )}
        </>
      )}
    </motion.section>
  );
}

function AnalystPanel({ state, onRetry }: { state: AnalysisState; onRetry: () => void }) {
  if (state.phase === "idle") return null;
  const ui = state.phase === "done" ? SEVERITY_UI[state.data.severity] : null;
  return (
    <motion.section initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .35 }} className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
      <div className="flex items-start justify-between gap-3">
        <div><span className="font-mono text-xs font-bold text-clay">RISK ANALYST</span><h3 className="mt-2 text-2xl font-semibold tracking-tight">Diagnostic de l'Analyste</h3></div>
        {ui && <span className={`flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-bold ${ui.badge}`}><ui.icon size={15} weight="fill" />{ui.label}</span>}
      </div>
      {state.phase === "loading" && <AnalystSkeleton />}
      {state.phase === "error" && (
        <div role="alert" className="mt-5 flex items-start justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div><p className="font-semibold">{ANALYST_ERROR}</p><p className="mt-1 text-xs text-red-600/80">{state.message}</p></div>
          <button onClick={onRetry} className="flex shrink-0 items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs font-bold hover:bg-red-100"><ArrowClockwise size={14} /> Réessayer</button>
        </div>
      )}
      {state.phase === "done" && ui && (
        <>
          <p className="mt-5 text-base leading-7 text-ink">{state.data.summary}</p>
          {state.data.robustness && <p className="mt-3 rounded-xl bg-paper px-4 py-3 text-sm leading-6 text-stone"><span className="font-mono text-xs font-bold text-ink">ROBUSTESSE · </span>{state.data.robustness}</p>}
          {state.data.drivers.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2">
              {state.data.drivers.map((d) => <span key={d.driver} title={d.comment} className="rounded-full border border-line bg-white px-3 py-1 text-xs font-semibold">{d.driver}{d.weightPct !== null && <span className="ml-1.5 font-mono text-clay">{euro.format(d.weightPct)} %</span>}</span>)}
            </div>
          )}
          {state.data.findings.length > 0 && (
            <ul className="mt-5 space-y-4">
              {state.data.findings.map((finding, i) => (
                <li key={`${finding.indicator}-${i}`} className="flex gap-3">
                  <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-paper ${ui.iconColor}`}><MagnifyingGlass size={16} weight="bold" /></span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2"><strong className="text-sm">{finding.indicator}</strong>{finding.evidence && <span className="rounded-full bg-slate-100 px-2.5 py-0.5 font-mono text-xs text-slate-600">{finding.evidence}</span>}</div>
                    {finding.interpretation && <p className="mt-1 text-sm leading-6 text-stone">{finding.interpretation}</p>}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {(state.data.orsa.length > 0 || state.data.mitigations.length > 0) && (
            <details className="mt-5 rounded-xl border border-line px-4 py-3 text-sm">
              <summary className="cursor-pointer font-mono text-xs font-bold text-stone">RECOMMANDATIONS ORSA &amp; PLANS D'ATTÉNUATION</summary>
              {state.data.orsa.length > 0 && <ul className="mt-3 list-disc space-y-1.5 pl-5 leading-6">{state.data.orsa.map((r) => <li key={r}>{r}</li>)}</ul>}
              {state.data.mitigations.length > 0 && <div className="mt-4 space-y-3">{state.data.mitigations.map((m) => <div key={m.lever} className="rounded-lg bg-paper p-3"><p className="font-bold">{m.lever}</p><p className="mt-1 text-stone">{m.description}</p>{m.expectedEffect && <p className="mt-1 text-xs text-clay">Effet attendu : {m.expectedEffect}</p>}</div>)}</div>}
            </details>
          )}
          {state.data.notes.length > 0 && <ul className="mt-4 space-y-1.5">{state.data.notes.map((n) => <li key={n} className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{n}</li>)}</ul>}
          {state.data.recommendedAction && (
            <div className="mt-6 flex gap-3 rounded-xl border border-blue-200 bg-blue-50 p-4">
              <Lightbulb size={22} weight="duotone" className="mt-0.5 shrink-0 text-clay" />
              <div><p className="font-mono text-xs font-bold text-clay">ACTION RECOMMANDÉE</p><p className="mt-1 text-sm font-medium leading-6">{state.data.recommendedAction}</p></div>
            </div>
          )}
        </>
      )}
    </motion.section>
  );
}

type EngineSource = "live" | "simulation" | "reported";

// Published figures of a prudential summary PDF (mirrors ReportedFigures in rag_service/extraction/qrt_pdf.py)
type ReportedFigures = {
  scr: number | null; eligible_funds: number | null; mcr: number | null; ratio_scr_pct: number | null; ratio_mcr_pct: number | null;
  bscr: number | null; diversification: number | null; modules: { name: string; value: number }[]; unit: string | null;
};
const MODULE_COLORS = ["#1E3A8A", "#2563EB", "#3B82F6", "#60A5FA", "#64748B"];

type PdfExtractNote = { method: "tables" | "vision"; pages: number; warnings: string[]; model: string | null; kind: "qrt" | "summary"; reported: ReportedFigures | null };
const isPdf = (file: File) => file.type === "application/pdf" || /\.pdf$/i.test(file.name);

// PDF import: the Python service returns { sheets } in the exact shape parseQrtWorkbook gives for an Excel file.
async function extractQrtFromPdf(file: File): Promise<{ sheets: QrtSheets } & PdfExtractNote> {
  const form = new FormData();
  form.append("file", file);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch("/api/n8n/qrt-extract", { method: "POST", body: form, signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.error === "string" ? data.error.replace(/^python: /, "") : `Extraction du PDF impossible (HTTP ${response.status}).`);
    if (!data.sheets || !Object.keys(data.sheets).length) throw new Error("Aucune ligne QRT trouvée dans ce PDF.");
    return { sheets: data.sheets as QrtSheets, method: data.method, pages: data.pages ?? 0, warnings: Array.isArray(data.warnings) ? data.warnings : [], model: data.model ?? null, kind: data.kind === "summary" ? "summary" : "qrt", reported: data.reported ?? null };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new Error("Extraction du PDF trop longue : réessayez avec un PDF plus court.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

// n8n answers with an array ([ { eligibleFunds, scr, ... } ]); a Code node may also nest it under `json`.
function unwrapScr(raw: unknown): Record<string, any> {
  let value: any = Array.isArray(raw) ? raw[0] : raw;
  if (value && typeof value === "object" && value.json && typeof value.json === "object" && value.eligibleFunds === undefined) value = value.json;
  return value && typeof value === "object" ? value : {};
}

const isEngineResult = (api: Record<string, any>) => [api.eligibleFunds, api.scr, api.mcr, api.ratio].every((n) => typeof n === "number" && Number.isFinite(n)) && Array.isArray(api.modules);
const engineWarnings = (api: Record<string, any>): string[] => (Array.isArray(api.quality?.warnings) ? api.quality.warnings : []).filter((w: unknown) => typeof w === "string" && !/export xlsx/i.test(w)); // the xlsx note is n8n-internal: this app builds the workbook itself

const REG_STATUS: Record<string, { label: string; cls: string }> = {
  COMPLIANT: { label: "Conforme", cls: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  WATCHLIST: { label: "Sous surveillance", cls: "border-orange-200 bg-orange-50 text-orange-700" },
  BREACH_SCR: { label: "Brèche SCR", cls: "border-red-200 bg-red-50 text-red-700" },
  BREACH_MCR: { label: "Brèche MCR", cls: "border-red-200 bg-red-50 text-red-700" },
};

// Every figure comes straight from the engine response: nothing is recomputed here.
function ScrBreakdown({ result }: { result: ScrResult }) {
  const d = result.details as Record<string, unknown> | undefined;
  if (!d) return null;
  const n = (key: string) => (typeof d[key] === "number" && Number.isFinite(d[key] as number) ? (d[key] as number) : null);
  const sumModules = result.modules.reduce((acc, m) => acc + m.value, 0);
  const haircut = n("tieringHaircut");
  type Line = [label: string, value: number | null, strong?: boolean, unit?: string];
  const bscr: Line[] = [...(result.status === "reported" ? [] : [["Somme des modules de risque", sumModules] as Line]), ["Bénéfice de diversification", n("diversificationBenefit")], ["Risque sur actifs incorporels", n("intangibles")], ["SCR de base (BSCR)", n("bscr"), true], ["Risque opérationnel", n("operationalRisk")], ["Absorption par les provisions techniques", n("lacTp")], ["Absorption par les impôts différés", n("lacDt")], ["SCR", result.scr, true]];
  const funds: Line[] = [["Fonds propres disponibles", n("availableOwnFunds")], ["Décote de classement (tiering)", haircut === null ? null : -haircut], ["Fonds propres éligibles (SCR)", result.eligibleFunds, true], ["MCR", result.mcr], ["Fonds propres éligibles (MCR)", n("eligibleFundsMcr")], ["Ratio SCR", result.ratio, true, "%"], ["Ratio MCR", n("mcrRatio"), false, "%"]];
  const status = REG_STATUS[String(d.regulatoryStatus ?? "")];
  const checks = (result.quality?.reconciliation ?? []) as Record<string, unknown>[];
  const warnings = (result.quality?.warnings ?? []).filter((w) => !/export xlsx/i.test(w));
  const table = (title: string, lines: Line[]) => (
    <div>
      <p className="mb-2 font-mono text-xs font-bold text-stone">{title}</p>
      <table className="w-full text-sm"><tbody>{lines.filter(([, v]) => v !== null).map(([label, value, strong, unit]) => (
        <tr key={label} className={`border-t border-line/70 ${strong ? "font-bold" : "text-stone"}`}><td className="py-2 pr-3">{label}</td><td className="py-2 text-right font-mono">{euro2.format(value as number)} {unit ?? "M€"}</td></tr>
      ))}</tbody></table>
    </div>
  );
  return (
    <section className="rounded-[20px] border border-line bg-white p-6 shadow-tactile">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-xs font-bold text-stone">{result.status === "reported" ? "CHIFFRES PUBLIÉS (NON RECALCULÉS)" : "DÉTAIL DU CALCUL"}</span>
        <div className="flex flex-wrap items-center gap-2">
          {d.tier1Compliant === false && <span className="rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-xs font-bold text-red-700">Tier 1 &lt; 50 % du SCR</span>}
          {status && <span className={`rounded-full border px-3 py-1 text-xs font-bold ${status.cls}`}>{status.label}</span>}
        </div>
      </div>
      <div className="mt-4 grid gap-6 md:grid-cols-2">{table("DU BSCR AU SCR", bscr)}{table("FONDS PROPRES ET RATIOS", funds)}</div>
      {checks.length > 0 && <div className="mt-5 flex flex-wrap gap-2">{checks.map((c, i) => { const ok = c.Statut === "OK"; return <span key={i} className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${ok ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}>{String(c.Indicateur)} : {ok ? "OK" : `écart ${euro2.format(Number(c["Écart (M€)"]))} M€`}</span>; })}</div>}
      {warnings.length > 0 && <ul className="mt-4 space-y-1.5">{warnings.map((w) => <li key={w} className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{w}</li>)}</ul>}
    </section>
  );
}

export type GeneratedReport = { url: string; createdAt: string; name: string };

export default function QrtAndScrView({ onTrace, simulation, onReportGenerated }: { onTrace: (agent: "Document Agent" | "SCR Agent", payload: string) => void; simulation: boolean; onReportGenerated?: (report: GeneratedReport) => void }) {
  const [rows, setRows] = useState<QrtRow[]>([]);
  const [sheets, setSheets] = useState<QrtSheets | null>(null);   // every sheet of the workbook, original headers: what the engine ingests
  const [result, setResult] = useState<ScrResult | null>(null);
  const [source, setSource] = useState<EngineSource>("live");
  const [isExtracting, setIsExtracting] = useState(false);
  const [extractNote, setExtractNote] = useState<PdfExtractNote | null>(null);   // set when the QRT came from a PDF
  const [fileName, setFileName] = useState("");
  const [fileError, setFileError] = useState("");
  const [analysis, setAnalysis] = useState<AnalysisState>({ phase: "idle" });
  const [report, setReport] = useState<ReportState>({ phase: "idle" });
  const [quality, setQuality] = useState<QualityState>({ phase: "idle" });
  const [toasts, setToasts] = useState<Toast[]>([]);
  const portfolioRef = useRef<Record<string, unknown> | null>(null);   // what the quality agent audited, re-sent to the analyst
  const inputRef = useRef<HTMLInputElement>(null);
  const analystRun = useRef(0);                         // ignores answers of superseded runs
  const lastSnapshot = useRef<Record<string, unknown> | null>(null);
  const pdfUrlRef = useRef<string | null>(null);
  const toastId = useRef(0);
  const isAnalyzing = analysis.phase === "loading";
  const isGeneratingReport = report.phase === "loading";
  const pdfUrl = report.phase === "done" ? report.url : null;
  const revokePdf = () => { if (pdfUrlRef.current) { URL.revokeObjectURL(pdfUrlRef.current); pdfUrlRef.current = null; } };
  useEffect(() => () => revokePdf(), []);
  const dismissToast = (id: number) => setToasts((current) => current.filter((toast) => toast.id !== id));
  const pushToast = (toast: Omit<Toast, "id">) => {
    const id = ++toastId.current;
    setToasts((current) => [...current.slice(-2), { ...toast, id }]);
    setTimeout(() => dismissToast(id), TOAST_MS);
  };
  // E-mail alerts through the n8n workflow (Gmail). Opt-in: off by default, so no mail leaves unless the user switches it on.
  const [emailAlerts, setEmailAlerts] = useState(false);
  const emailAlertsRef = useRef(false);
  const qualityRef = useRef<QualityReport | null>(null);
  const notify = async (type: "alert" | "report_ready", details: Record<string, unknown>) => {
    if (!emailAlertsRef.current) return;
    try {
      const response = await fetch("/api/n8n/notify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, details }) });
      if (!response.ok) throw new Error(`Erreur HTTP ${response.status}`);
      pushToast({ emoji: "📧", title: type === "alert" ? "Alerte e-mail transmise" : "Notification e-mail transmise", text: "Demande envoyée au workflow n8n (Gmail).", tone: "success" });
    } catch (error) {
      pushToast({ emoji: "📭", title: "E-mail non envoyé", text: failureMessage(error, "Délai dépassé."), tone: "error" });
    }
  };
  const resetCascade = () => { analystRun.current += 1; lastSnapshot.current = null; portfolioRef.current = null; qualityRef.current = null; revokePdf(); setQuality({ phase: "idle" }); setAnalysis({ phase: "idle" }); setReport({ phase: "idle" }); };
  // Step 0: data quality gate (Python agent, no LLM). Resolves to the report, or null if the service is unreachable (never blocks on its own failure).
  const auditQuality = async (portfolio: Record<string, unknown>, id: number): Promise<QualityReport | null> => {
    setQuality({ phase: "loading" });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch("/api/n8n/pipeline", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ portfolio }), signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `Erreur HTTP ${response.status}`);
      if (!data.data_quality) throw new Error("Réponse de l'audit illisible.");
      if (id !== analystRun.current) return null;
      setQuality({ phase: "done", report: data.data_quality as QualityReport });
      qualityRef.current = data.data_quality as QualityReport;
      return data.data_quality as QualityReport;
    } catch (error) {
      if (id !== analystRun.current) return null;
      const message = failureMessage(error, "délai dépassé");
      setQuality({ phase: "error", message });
      pushToast({ emoji: "🛡️", title: "Audit qualité indisponible", text: "Le calcul continue sans contrôle préalable des données.", tone: "error" });
      return null;
    } finally {
      clearTimeout(timer);
    }
  };
  const loadFile = async (file?: File) => {
    if (!file) return;
    setFileError("");
    setFileName(file.name);
    try {
      // Excel: read in the browser, unchanged. PDF: extracted by the Python service into the very same `sheets` shape.
      let parsedSheets: QrtSheets;
      if (isPdf(file)) {
        setIsExtracting(true);
        const extraction = await extractQrtFromPdf(file);
        parsedSheets = extraction.sheets;
        setExtractNote({ method: extraction.method, pages: extraction.pages, warnings: extraction.warnings, model: extraction.model, kind: extraction.kind, reported: extraction.reported });
      } else {
        parsedSheets = parseQrtWorkbook(await file.arrayBuffer());
        setExtractNote(null);
      }
      const parsedRows = qrtRowsFromSheets(parsedSheets);
      setSheets(parsedSheets);
      setRows(parsedRows);
      setResult(null);
      resetCascade();
      onTrace("Document Agent", `QRT importé: ${file.name} (${parsedRows.length} lignes)`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Impossible de lire ce fichier.";
      setExtractNote(null);
      setFileError(message);
      setSheets(null);
      setRows([]);
      setResult(null);
      resetCascade();
      onTrace("Document Agent", `Échec import ${file.name}: ${message}`);
    } finally {
      setIsExtracting(false);
    }
  };
  // Step 2: the analyst receives the figures from step 1. Never throws: resolves to the diagnosis, or null after showing the error.
  const askAnalyst = async (snapshot: Record<string, unknown>, id: number): Promise<Analysis | null> => {
    lastSnapshot.current = snapshot;
    setAnalysis({ phase: "loading" });
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ANALYST_TIMEOUT_MS);
    try {
      // Live: the LangGraph pipeline (Python: quality gate + Gemini analyst). Simulation: the n8n simulated risk-agent, as before.
      const useGraph = !simulation && portfolioRef.current !== null;
      const response = await fetch(useGraph ? "/api/n8n/pipeline" : "/api/n8n/risk-agent", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify(useGraph ? { portfolio: portfolioRef.current, solvency: snapshot } : { ...snapshot, simulation }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : typeof data.detail === "string" ? data.detail : `Erreur HTTP ${response.status}`);
      if (data.data_quality) setQuality({ phase: "done", report: data.data_quality as QualityReport });
      if (data.pipeline_status === "ANALYSIS_FAILED") throw new Error(strings(data.errors).join(" | ") || "L'analyste n'a pas répondu.");
      if (data.pipeline_status === "BLOCKED") throw new Error("Les données ont été bloquées par l'audit qualité.");
      const parsed = parseAnalysis(data);
      if (!parsed) throw new Error("Réponse de l'analyste illisible (JSON inattendu).");
      if (id !== analystRun.current) return null;
      setAnalysis({ phase: "done", data: parsed });
      pushToast({ emoji: "🕵️‍♂️", title: "Analyse terminée !", text: "Le diagnostic de l'analyste est prêt. Le rapport PDF se prépare…", tone: "success" });
      onTrace("SCR Agent", JSON.stringify({ analyst: parsed.severity, latency: Math.round(performance.now() - started) }));
      return parsed;
    } catch (error) {
      if (id !== analystRun.current) return null;
      const message = failureMessage(error, "Délai dépassé : l'analyste n'a pas répondu à temps.");
      setAnalysis({ phase: "error", message });
      pushToast({ emoji: "😵‍💫", title: "L'analyste a calé", text: "Les résultats SCR restent valides. Vous pouvez relancer le diagnostic.", tone: "error" });
      onTrace("SCR Agent", JSON.stringify({ analyst: "error", error: message }));
      return null;
    } finally {
      clearTimeout(timer);
    }
  };
  // Step 3: the Python service renders the audit PDF from the SCR figures + the diagnosis.
  const generateReport = async (snapshot: Record<string, unknown>, analyst: Analysis, id: number) => {
    revokePdf();
    setReport({ phase: "loading" });
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REPORT_TIMEOUT_MS);
    try {
      const analystData = { status: analyst.status, severity: analyst.severity, summary: analyst.summary, findings: analyst.findings, recommended_action: analyst.recommendedAction };
      const response = await fetch("/api/n8n/report-pdf", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scrData: snapshot, analystData }), signal: controller.signal });
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(typeof data.error === "string" ? data.error : `Erreur HTTP ${response.status}`); }
      const blob = await response.blob();
      if (!blob.size || !blob.type.includes("pdf")) throw new Error("Le service n'a pas renvoyé de PDF.");
      if (id !== analystRun.current) return;
      const url = URL.createObjectURL(blob);
      pdfUrlRef.current = url;
      setReport({ phase: "done", url });
      onReportGenerated?.({ url, createdAt: new Date().toISOString(), name: `SCR report · ${new Date().toLocaleDateString("fr-FR")}` });
      void notify("report_ready", { ratio: snapshot.ratio, scr: snapshot.scr, eligible_funds: snapshot.eligibleFunds, mcr: snapshot.mcr, severity: analyst.severity, report_name: `Rapport SCR du ${new Date().toLocaleDateString("fr-FR")}` });
      pushToast({ emoji: "📄✨", title: "Rapport PDF prêt !", text: "Cliquez sur « Télécharger le rapport PDF » pour le récupérer.", tone: "success" });
      onTrace("SCR Agent", JSON.stringify({ report: "pdf", bytes: blob.size, latency: Math.round(performance.now() - started) }));
    } catch (error) {
      if (id !== analystRun.current) return;
      const message = failureMessage(error, "Délai dépassé : la génération du PDF a pris trop de temps.");
      setReport({ phase: "error", message });
      pushToast({ emoji: "⚠️", title: "Rapport PDF indisponible", text: message, tone: "error" });
      onTrace("SCR Agent", JSON.stringify({ report: "error", error: message }));
    } finally {
      clearTimeout(timer);
    }
  };
  const cascade = async (snapshot: Record<string, unknown>, id: number) => {
    const analyst = await askAnalyst(snapshot, id);
    if (analyst?.severity === "high" && id === analystRun.current) {
      const q = qualityRef.current;
      void notify("alert", { quality_status: q?.status, quality_score: q?.quality_score, critical_count: q?.counts_by_severity.CRITICAL ?? 0, ratio: snapshot.ratio, severity: analyst.severity, summary: analyst.summary, recommended_action: analyst.recommendedAction });
    }
    if (analyst && id === analystRun.current) await generateReport(snapshot, analyst, id);
  };
  const retryAnalyst = () => { if (lastSnapshot.current) void cascade(lastSnapshot.current, ++analystRun.current); };
  const retryReport = () => { if (lastSnapshot.current && analysis.phase === "done") void generateReport(lastSnapshot.current, analysis.data, analystRun.current); };
  const run = async () => {
    const id = ++analystRun.current;
    revokePdf();
    setAnalysis({ phase: "idle" });
    setReport({ phase: "idle" });
    const started = performance.now();
    // A prudential summary (no QRT code) is not recomputed: its published figures are audited, then analysed as they are.
    if (extractNote?.kind === "summary" && extractNote.reported) {
      await runReported(extractNote.reported, id);
      return;
    }
    // Step 0: data quality gate on the QRT rows, before anything is calculated.
    let payload: ReturnType<typeof buildScrPayload>;
    try {
      if (!sheets) throw new Error("Chargez d'abord un fichier QRT.");
      payload = buildScrPayload(sheets, { simulation });
    } catch (error) {
      pushToast({ emoji: "⚠️", title: "Fichier QRT inutilisable", text: failureMessage(error, "Délai dépassé."), tone: "error" });
      return;
    }
    const portfolio: Record<string, unknown> = {
      source: { system: "SolvaRisk – import Excel", extraction_date: new Date().toISOString().slice(0, 10), reference: fileName || undefined },
      positions: Object.entries(payload.sheets).flatMap(([name, list]) => list.map((row) => ({ _sheet: name, ...row }))),
    };
    portfolioRef.current = portfolio;
    const audit = await auditQuality(portfolio, id);
    if (id !== analystRun.current) return;
    if (audit?.status === "BLOCKED") {
      setResult(null);
      pushToast({ emoji: "🛑", title: "Calcul suspendu", text: `${audit.counts_by_severity.CRITICAL ?? 0} anomalie(s) critique(s) dans les données : corrigez-les puis relancez.`, tone: "error" });
      onTrace("Document Agent", JSON.stringify({ quality: "BLOCKED", score: audit.quality_score, critical: audit.counts_by_severity.CRITICAL ?? 0 }));
      void notify("alert", { quality_status: audit.status, quality_score: audit.quality_score, critical_count: audit.counts_by_severity.CRITICAL ?? 0 });
      return;
    }
    if (audit) onTrace("Document Agent", JSON.stringify({ quality: audit.status, score: audit.quality_score, rows: audit.rows_audited }));

    // Step 1: maths. Shown immediately, the next steps never block it.
    let computed: ScrResult;
    let api: Record<string, any>;
    try {
      const response = await fetch("/api/n8n/scr-agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      api = unwrapScr(await response.json().catch(() => ({})));
      // Live mode shows the engine's own numbers or an error: never the proxy's demo fallback.
      if (!simulation && api.status === "simulation-fallback") throw new Error(`Le moteur n8n est injoignable. ${typeof api.error === "string" ? api.error : ""}`.trim());
      if (!isEngineResult(api)) throw new Error(`Réponse inattendue du moteur SCR${typeof api.message === "string" ? ` : ${api.message}` : "."}`);
      computed = simulation ? (api as ScrResult) : assertValidScrResult(api as ScrResult);
    } catch (error) {
      pushToast({ emoji: "⚠️", title: "Calcul SCR impossible", text: failureMessage(error, "Délai dépassé."), tone: "error" });
      onTrace("SCR Agent", JSON.stringify({ error: failureMessage(error, "timeout") }));
      return;
    }
    setResult(computed);
    setSource(api.status === "simulation" ? "simulation" : "live");
    onTrace("SCR Agent", JSON.stringify({ sheets: Object.keys(payload.sheets).length, mode: api.status ?? (simulation ? "simulation" : "live"), latency: Math.round(performance.now() - started) }));
    const { ratio, scr, mcr, eligibleFunds, modules } = computed;
    void cascade({ ratio, scr, mcr, eligibleFunds, modules, details: api.details ?? {}, warnings: engineWarnings(api) }, id);
  };
  // Summary documents: quality agent in summary mode (consistency of the published figures), no SCR engine, then the cascade.
  const runReported = async (figures: ReportedFigures, id: number) => {
    const portfolio: Record<string, unknown> = {
      document_kind: "summary",
      source: { system: "SolvaRisk – import PDF (synthèse prudentielle)", extraction_date: new Date().toISOString().slice(0, 10), reference: fileName || undefined },
      positions: [figures],
    };
    portfolioRef.current = portfolio;
    const audit = await auditQuality(portfolio, id);
    if (id !== analystRun.current) return;
    if (audit?.status === "BLOCKED") {
      setResult(null);
      pushToast({ emoji: "🛑", title: "Chiffres publiés inutilisables", text: `${audit.counts_by_severity.CRITICAL ?? 0} anomalie(s) critique(s) : SCR ou fonds propres absents du document.`, tone: "error" });
      void notify("alert", { quality_status: audit.status, quality_score: audit.quality_score, critical_count: audit.counts_by_severity.CRITICAL ?? 0 });
      return;
    }
    const { scr, eligible_funds: funds, mcr } = figures;
    if (!scr || scr <= 0 || funds === null) {
      pushToast({ emoji: "⚠️", title: "Chiffres publiés incomplets", text: "Le SCR ou les fonds propres éligibles manquent dans le document.", tone: "error" });
      return;
    }
    const ratio = figures.ratio_scr_pct ?? (funds / scr) * 100;
    const mcrRatio = figures.ratio_mcr_pct ?? (mcr ? (funds / mcr) * 100 : null);
    const regulatoryStatus = mcrRatio !== null && mcrRatio < 100 ? "BREACH_MCR" : ratio < 100 ? "BREACH_SCR" : ratio < 130 ? "WATCHLIST" : "COMPLIANT";
    const computed: ScrResult = {
      eligibleFunds: funds, scr, mcr: mcr ?? 0, ratio,
      modules: figures.modules.map((m, i) => ({ name: m.name, value: m.value, color: MODULE_COLORS[i % MODULE_COLORS.length] })),
      details: { bscr: figures.bscr ?? undefined, diversificationBenefit: figures.diversification ?? undefined, mcrRatio: mcrRatio ?? undefined, regulatoryStatus, source: "reported" },
      quality: { warnings: [], reconciliation: [], engine: "reported" },
      status: "reported",
    };
    setResult(computed);
    setSource("reported");
    onTrace("SCR Agent", JSON.stringify({ mode: "reported", scr, ratio: Math.round(ratio * 100) / 100 }));
    void cascade({ ratio, scr, mcr: computed.mcr, eligibleFunds: funds, modules: computed.modules, details: computed.details,
      warnings: ["Chiffres publiés par un document de synthèse, non recalculés par la formule standard."] }, id);
  };
  const metrics = result ? [["Fonds propres éligibles", `${euro2.format(result.eligibleFunds)} M€`, "text-clay"], ["SCR standard", `${euro2.format(result.scr)} M€`, "text-sage"], ["MCR", `${euro2.format(result.mcr)} M€`, "text-ochre"], ["Ratio de solvabilité", `${euro2.format(result.ratio)} %`, "text-ink"]] : [];
  const maxModule = result ? Math.max(1, ...result.modules.map((m) => m.value)) : 1;
  return (
    <div>
      <div className="mb-6 grid grid-cols-3 gap-2 rounded-2xl border border-line bg-white p-2 shadow-tactile sm:grid-cols-5">
        {[["01", "Import QRT", true], ["02", "Validation", quality.phase === "done" && quality.report.status !== "BLOCKED"], ["03", "Calcul SCR", Boolean(result)], ["04", "Diagnostic", analysis.phase === "done"], ["05", "Rapport", Boolean(pdfUrl)]].map(([number, label, complete]) => <div key={label as string} className={`flex items-center gap-2 rounded-xl px-3 py-2.5 ${complete ? "bg-blue-50" : "bg-transparent"}`}><span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full font-mono text-[10px] font-bold ${complete ? "bg-clay text-white" : "bg-paper text-stone"}`}>{number}</span><span className={`hidden text-xs font-semibold sm:block ${complete ? "text-ink" : "text-stone"}`}>{label}</span></div>)}
      </div>
      <div className="grid gap-6 lg:grid-cols-[.86fr_1.14fr]">
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
      <div className="space-y-5">
        <div className="relative rounded-[20px] border border-line bg-white p-6 shadow-tactile">
          <div className="flex items-start justify-between"><div><span className="font-mono text-xs font-bold text-clay">DOCUMENT AGENT</span><h2 className="mt-2 text-3xl font-semibold tracking-tight">Importer QRT</h2></div><FileArrowUp size={28} weight="duotone" className="text-clay" /></div>
          <button type="button" onClick={() => inputRef.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void loadFile(event.dataTransfer.files[0]); }} className="mt-6 flex w-full flex-col items-center justify-center rounded-[15px] border border-dashed border-clay/50 bg-blue-50 px-4 py-10 text-center transition hover:-translate-y-0.5 hover:border-clay hover:shadow-sm"><UploadSimple size={29} className="text-clay" /><span className="mt-3 text-base font-bold">Déposer un fichier .xlsx ou .pdf</span><span className="mt-1 text-sm text-stone">ou cliquer pour parcourir</span></button>
          <input ref={inputRef} type="file" accept=".xlsx,.xls,.csv,.pdf" className="hidden" onChange={(event) => { void loadFile(event.target.files?.[0]); event.currentTarget.value = ""; }} />
          {isExtracting && <p role="status" className="mt-3 flex animate-pulse items-center gap-2 rounded-lg bg-blue-50 px-3 py-2 text-sm text-clay"><SpinnerGap size={16} className="animate-spin" /> Extraction du PDF en cours…</p>}
          {fileName && !fileError && !isExtracting && <p className="mt-3 rounded-lg bg-blue-50 px-3 py-2 text-sm text-clay">Fichier chargé : <strong>{fileName}</strong></p>}
          {extractNote && !fileError && !isExtracting && (
            <div className={`mt-2 rounded-lg border px-3 py-2 text-xs ${extractNote.method === "vision" || extractNote.kind === "summary" ? "border-amber-200 bg-amber-50 text-amber-800" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`}>
              {extractNote.kind === "summary" && <p className="mb-1 font-semibold">Document de synthèse prudentielle : chiffres publiés repris tels quels{extractNote.reported ? "" : " (lecture des chiffres impossible)"}</p>}<p className="font-semibold">{extractNote.method === "vision" ? `PDF lu par IA${extractNote.model ? ` (${extractNote.model})` : ""}` : "Tableaux du PDF lus directement"} · {extractNote.pages} page(s)</p>
              {extractNote.warnings.map((w) => <p key={w} className="mt-1">{w}</p>)}
            </div>
          )}
          {fileError && <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{fileError}</p>}
        </div>
        <div className="overflow-hidden rounded-[20px] border border-line bg-white shadow-tactile"><div className="flex items-center justify-between border-b border-line px-5 py-4"><div><span className="font-mono text-xs font-bold text-stone">QRT</span><p className="mt-1 text-sm text-stone">{rows.length} lignes</p></div><span className="rounded-full bg-blue-50 px-2.5 py-1 font-mono text-xs text-clay">S.23 / S.25</span></div>{rows.length === 0 ? <div className="flex min-h-32 items-center justify-center px-5 text-center text-sm text-stone">Aucune donnée importée. Chargez un fichier QRT pour afficher l’aperçu.</div> : <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-paper font-mono text-xs text-stone"><tr><th className="px-5 py-3">CODE</th><th className="px-5 py-3">LIBELLÉ</th><th className="px-5 py-3 text-right">VALEUR</th></tr></thead><tbody>{rows.map((row, i) => <tr key={`${row.code}-${i}`} className="border-t border-line/70"><td className="px-5 py-3 font-mono text-xs text-clay">{row.code}</td><td className="px-5 py-3">{row.libelle}</td><td className="px-5 py-3 text-right font-mono">{euro.format(row.valeur)} {row.unite}</td></tr>)}</tbody></table></div>}</div>
      </div>
      <div className="space-y-5">
        <div className="flex items-end justify-between gap-4"><div><span className="font-mono text-xs font-bold text-sage">SCR AGENT</span><h2 className="mt-2 text-3xl font-semibold tracking-tight">Calcul</h2></div><motion.button whileTap={{ scale: .97 }} onClick={run} disabled={rows.length === 0} className="flex items-center gap-2 rounded-full bg-ink px-5 py-3 text-sm font-bold text-white shadow-sm hover:bg-clay disabled:cursor-not-allowed disabled:opacity-40"><Lightning size={18} weight="fill" /> Calculer</motion.button></div>
        <label className="flex w-fit cursor-pointer items-center gap-2 text-xs text-stone"><input type="checkbox" checked={emailAlerts} onChange={(event) => { emailAlertsRef.current = event.target.checked; setEmailAlerts(event.target.checked); }} className="h-3.5 w-3.5 accent-clay" />Alertes e-mail (n8n) : anomalie critique, rapport prêt</label><QualityPanel state={quality} />{result ? <><div className="grid grid-cols-2 gap-3">{metrics.map(([label, value, color]) => <div key={label} className="rounded-[15px] border border-line bg-white p-5 shadow-tactile"><p className="text-sm text-stone">{label}</p><p className={`mt-3 font-mono text-2xl font-bold ${color}`}>{value}</p></div>)}</div><div className="rounded-[20px] border border-line bg-white p-6 shadow-tactile"><div className="flex justify-between"><span className="font-mono text-xs font-bold text-stone">BSCR</span><span className={`font-mono text-xs ${source === "live" ? "text-sage" : source === "reported" ? "text-clay" : "text-ochre"}`}>{source === "live" ? "MOTEUR n8n · LIVE" : source === "reported" ? "CHIFFRES PUBLIÉS (PDF) · NON RECALCULÉS" : "SIMULATION (valeurs de démo)"}</span></div><div className="mt-6 space-y-4">{result.modules.map((module) => <div key={module.name}><div className="mb-1 flex justify-between text-sm"><span>{module.name}</span><span className="font-mono text-stone">{euro2.format(module.value)} M€</span></div><div className="h-2 overflow-hidden rounded-full bg-paper"><motion.div initial={{ width: 0 }} animate={{ width: `${(module.value / maxModule) * 100}%` }} transition={{ duration: .7 }} className="h-full rounded-full" style={{ backgroundColor: module.color }} /></div></div>)}</div><div className="mt-7 flex flex-wrap items-center gap-3"><button onClick={() => downloadOutput(result)} className="flex items-center gap-2 rounded-lg border border-line px-4 py-2.5 text-sm font-bold hover:bg-paper"><ArrowDown size={18} /> Exporter Excel</button>{pdfUrl ? <motion.a initial={{ scale: .9, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} href={pdfUrl} download={`Rapport_SCR_${new Date().toISOString().slice(0, 10)}.pdf`} className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-bold text-white shadow-sm hover:bg-emerald-700"><FilePdf size={18} weight="fill" /> Télécharger le rapport PDF</motion.a> : report.phase === "error" ? <button onClick={retryReport} disabled={analysis.phase !== "done"} className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm font-bold text-red-700 hover:bg-red-100 disabled:opacity-50"><ArrowClockwise size={18} /> Réessayer le rapport PDF</button> : <button disabled aria-busy={isAnalyzing || isGeneratingReport} className="flex cursor-not-allowed items-center gap-2 rounded-lg border border-line bg-paper px-4 py-2.5 text-sm font-bold text-stone"><>{isAnalyzing || isGeneratingReport ? <SpinnerGap size={18} className="animate-spin" /> : <FilePdf size={18} />}{isGeneratingReport ? "Génération du rapport…" : isAnalyzing ? "En attente de l'analyse…"         : "Rapport PDF"}</></button>}</div></div><ScrBreakdown result={result} /><AnalystPanel state={analysis} onRetry={retryAnalyst} /></> : quality.phase === "done" && quality.report.status === "BLOCKED" ? null : <div className="min-h-[310px] rounded-[20px] border border-dashed border-line bg-white p-8 shadow-tactile"><div className="flex h-full min-h-[250px] flex-col items-center justify-center text-center"><div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-clay"><Lightning size={27} weight="duotone" /></div><h3 className="mt-4 text-lg font-bold">SCR engine ready</h3><p className="mt-2 max-w-xs text-sm leading-6 text-stone">{rows.length > 0 ? "Vos données QRT sont prêtes. Lancez le calcul pour obtenir les indicateurs, les contributions par module et le diagnostic." : "Importez un fichier QRT pour activer le moteur SCR et afficher les résultats réels."}</p>{rows.length > 0 && <button onClick={run} className="mt-5 rounded-xl bg-ink px-4 py-2.5 text-sm font-bold text-white hover:bg-clay">Calculer maintenant</button>}</div></div>}
      </div>
      </div>
    </div>
  );
}
