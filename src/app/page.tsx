"use client";

import { ChartLineUp, Gauge, House, FileText, Robot, Scales, CloudRain, SlidersHorizontal, GearSix, ToggleLeft, ToggleRight, Bell, ArrowUpRight } from "@phosphor-icons/react";
import { useState } from "react";
import CopilotAndStressView from "@/components/CopilotAndStressView";
import EiopaAgentView from "@/components/EiopaAgentView";
import ClimateAgentView from "@/components/ClimateAgentView";
import QrtAndScrView from "@/components/QrtAndScrView";
import AgentPipelineStrip, { type Trace } from "@/components/AgentPipelineStrip";
import ReportHistory from "@/components/ReportHistory";
import type { GeneratedReport } from "@/components/QrtAndScrView";

type Tab = "qrt" | "rag" | "eiopa" | "climate" | "stress" | "reports";
const tabs: { id: Tab; label: string }[] = [
  { id: "qrt", label: "QRT & SCR" },
  { id: "rag", label: "Copilot" },
  { id: "eiopa", label: "Agent EIOPA" },
  { id: "climate", label: "Climat & ESG" },
  { id: "stress", label: "Stress test" },
  { id: "reports", label: "Reports" },
];

export default function Home() {
  const [tab, setTab] = useState<Tab>("qrt");
  const [live, setLive] = useState(true);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [reports, setReports] = useState<GeneratedReport[]>([]);
  const addTrace = (agent: Trace["agent"], payload: string) => {
    setTraces((current) => [...current, { agent, payload, latency: 0, status: "terminé" }]);
  };
  const activeAgent = tab === "qrt" ? "Document Agent" : tab === "rag" || tab === "eiopa" || tab === "climate" ? "RAG Agent" : tab === "reports" ? "Report Agent" : "Risk Analyst Agent";

  return (
    <main className="min-h-screen bg-paper">
      <div className="flex min-h-screen">
        <aside className="no-print hidden w-64 shrink-0 border-r border-line bg-white px-4 py-5 lg:flex lg:flex-col">
          <div className="flex items-center gap-3 px-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-ink text-white"><ChartLineUp size={19} weight="duotone" /></div>
            <div><p className="font-bold tracking-tight">SolvaRisk</p><p className="font-mono text-[9px] uppercase tracking-widest text-stone">Risk cockpit</p></div>
          </div>
          <p className="mt-10 px-3 font-mono text-[10px] font-bold uppercase tracking-[.16em] text-stone">Workspace</p>
          <nav className="mt-3 space-y-1">
            {[
              { id: "qrt" as Tab, label: "QRT & SCR", icon: Gauge },
              { id: "rag" as Tab, label: "Copilot", icon: Robot },
              { id: "eiopa" as Tab, label: "Agent EIOPA", icon: Scales },
              { id: "climate" as Tab, label: "Climat & ESG", icon: CloudRain },
              { id: "stress" as Tab, label: "Stress testing", icon: SlidersHorizontal },
              { id: "reports" as Tab, label: "Report history", icon: FileText },
            ].map((item) => { const Icon = item.icon; return <button key={item.id} onClick={() => setTab(item.id)} className={`flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-sm font-semibold transition ${tab === item.id ? "bg-blue-50 text-clay" : "text-stone hover:bg-paper hover:text-ink"}`}><Icon size={19} weight={tab === item.id ? "fill" : "regular"} />{item.label}{tab === item.id && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-clay" />}</button>; })}
          </nav>
          <p className="mt-9 px-3 font-mono text-[10px] font-bold uppercase tracking-[.16em] text-stone">System</p>
          <nav className="mt-3 space-y-1 text-sm font-semibold text-stone">
            <button className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left hover:bg-paper hover:text-ink"><GearSix size={19} /> Settings</button>
          </nav>
          <div className="mt-auto rounded-2xl bg-paper p-4"><p className="text-xs font-bold">Data sources</p><div className="mt-3 flex items-center gap-2 text-xs text-stone"><span className="h-2 w-2 rounded-full bg-stone/40" />Awaiting QRT import</div><p className="mt-2 font-mono text-[9px] uppercase text-stone">No file loaded</p></div>
        </aside>
        <div className="min-w-0 flex-1">
          <header className="no-print border-b border-line bg-white">
            <div className="mx-auto flex max-w-[1440px] items-center justify-between px-5 py-4 sm:px-8 lg:px-12">
              <div className="flex items-center gap-3 lg:hidden"><div className="flex h-8 w-8 items-center justify-center rounded-lg bg-ink text-white"><ChartLineUp size={17} weight="duotone" /></div><span className="font-bold">SolvaRisk</span></div>
              <div className="hidden items-center gap-2 text-sm lg:flex"><House size={16} className="text-stone" /> <span className="text-stone">Solva Mutual</span><span className="text-line">/</span><strong>FY 2026</strong></div>
              <div className="flex items-center gap-3"><button aria-label="Notifications" className="rounded-lg p-2 text-stone hover:bg-paper hover:text-ink"><Bell size={18} /></button><button onClick={() => setLive(!live)} className="flex items-center gap-2 rounded-full border border-line bg-white px-3 py-1.5 text-xs font-semibold shadow-sm">{live ? <ToggleRight size={20} className="text-emerald-600" weight="fill" /> : <ToggleLeft size={20} className="text-stone" weight="fill" />}{live ? "Live" : "Simulation"}</button></div>
            </div>
          </header>
          <div className="mx-auto max-w-[1440px] px-5 pb-6 pt-7 sm:px-8 lg:px-12">
            <div className="flex items-end justify-between gap-6">
              <div><p className="font-mono text-[10px] font-bold uppercase tracking-[.18em] text-clay">Solvency II · 2026</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em] sm:text-5xl">{tab === "qrt" ? "Portfolio health" : tab === "rag" ? "Regulatory intelligence" : tab === "eiopa" ? "EIOPA & QRT expert" : tab === "climate" ? "Climate & ESG stress" : tab === "reports" ? "Report history" : "Scenario analysis"}</h1><p className="mt-2 max-w-xl text-sm text-stone">{tab === "qrt" ? "Importez vos données, validez les entrées et calculez la solvabilité." : tab === "rag" ? "Interrogez votre base réglementaire avec un contexte de risque partagé." : tab === "eiopa" ? "Vérifiez les limites par Tier avec la base réglementaire et générez l’état S.23.01 en Excel." : tab === "climate" ? "Croisez le portefeuille Non-Vie avec les données climatiques réelles pour mesurer le SCR exposé aux inondations." : tab === "reports" ? "Retrouvez les trois derniers rapports générés automatiquement par le reporting agent." : "Mesurez l’impact de chocs combinés sur votre ratio de solvabilité."}</p></div>
              <button className="hidden items-center gap-2 rounded-xl border border-line bg-white px-4 py-2.5 text-sm font-bold shadow-sm hover:border-clay sm:flex"><ArrowUpRight size={16} /> Export center</button>
            </div>
          </div>
          <div className="lg:hidden"><nav className="mx-5 flex gap-1 overflow-x-auto rounded-xl border border-line bg-white p-1 sm:mx-8">{tabs.map((item) => <button key={item.id} onClick={() => setTab(item.id)} className={`flex-1 whitespace-nowrap rounded-lg px-4 py-2.5 text-sm font-semibold transition ${tab === item.id ? "bg-blue-600 text-white" : "text-stone hover:bg-paper hover:text-ink"}`}>{item.label}</button>)}</nav></div>
          <AgentPipelineStrip activeAgent={activeAgent} traces={traces} />
          <section className="mx-auto max-w-[1440px] px-5 py-7 sm:px-8 lg:px-12">
        {tab === "qrt" && <QrtAndScrView simulation={!live} onTrace={addTrace} onReportGenerated={(report) => setReports((current) => [report, ...current.filter((item) => item.url !== report.url)].slice(0, 3))} />}
        {tab === "rag" && <CopilotAndStressView mode="rag" live={live} onTrace={addTrace} />}
        {tab === "eiopa" && <EiopaAgentView onTrace={addTrace} />}
        {tab === "climate" && <ClimateAgentView onTrace={addTrace} />}
        {tab === "stress" && <CopilotAndStressView mode="stress" live={live} onTrace={addTrace} />}
        {tab === "reports" && <ReportHistory reports={reports} />}
          </section>
          <footer className="no-print mx-auto flex max-w-[1440px] justify-between px-5 pb-7 pt-6 font-mono text-[9px] uppercase tracking-wider text-stone sm:px-8 lg:px-12"><span>SolvaRisk · Risk cockpit</span><span>S.23 · S.25 · EIOPA</span></footer>
        </div>
      </div>
    </main>
  );
}
