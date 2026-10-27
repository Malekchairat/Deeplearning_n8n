"use client";

import { CaretDown, Code, Database, FileText, Pulse, Robot } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import type { AgentName } from "@/lib/solvarisk";

export type Trace = { agent: AgentName; latency: number; payload: string; status: string };
const agents: { name: AgentName; label: string; detail: string; icon: typeof Robot; tone: string }[] = [
  { name: "Document Agent", label: "Document", detail: "QRT S.25 / S.23", icon: FileText, tone: "text-clay" },
  { name: "SCR Agent", label: "SCR", detail: "moteur Python", icon: Code, tone: "text-sage" },
  { name: "RAG Agent", label: "RAG", detail: "ChromaDB", icon: Database, tone: "text-clay" },
  { name: "Risk Analyst Agent", label: "Risk analyst", detail: "stress testing", icon: Pulse, tone: "text-sage" },
  { name: "Report Agent", label: "Report", detail: "PDF / Power BI", icon: Robot, tone: "text-ochre" },
];

export default function AgentPipelineStrip({ activeAgent, traces }: { activeAgent?: AgentName; traces: Trace[] }) {
  const [open, setOpen] = useState(false);
  return <section className="no-print border-y border-line bg-white"><div className="mx-auto max-w-[1440px] px-5 py-3 sm:px-8 lg:px-12"><div className="flex items-center justify-between"><div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-sage" /><span className="font-mono text-[10px] font-bold uppercase tracking-[.16em] text-stone">Agents</span></div><button onClick={() => setOpen(!open)} className="flex items-center gap-1.5 rounded-full border border-line bg-paper px-2.5 py-1.5 font-mono text-[10px] font-bold text-stone hover:text-ink">Logs [{traces.length.toString().padStart(2, "0")}]<CaretDown size={13} className={open ? "rotate-180" : ""} /></button></div><div className="mt-3 flex items-center gap-1 overflow-x-auto pb-1">{agents.map((agent, index) => { const Icon = agent.icon; const isActive = activeAgent === agent.name; return <div key={agent.name} className="flex min-w-[142px] items-center gap-1.5"><motion.div animate={isActive ? { y: [0, -2, 0] } : {}} transition={{ repeat: Infinity, duration: 1.2 }} className={`flex flex-1 items-center gap-2 rounded-[10px] border bg-white px-2.5 py-2 ${isActive ? "border-clay shadow-sm" : "border-line"}`}><Icon size={16} weight="duotone" className={agent.tone} /><div><p className="text-[11px] font-bold leading-none">{agent.label}</p><p className="mt-1 font-mono text-[8px] uppercase text-stone">{agent.detail}</p></div>{isActive && <motion.span className="ml-auto h-1.5 w-1.5 rounded-full bg-clay" animate={{ opacity: [1, .25, 1] }} transition={{ repeat: Infinity, duration: 1 }} />}</motion.div>{index < agents.length - 1 && <span className="text-stone/50">·</span>}</div>; })}</div><AnimatePresence>{open && <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden"><div className="mt-3 border-t border-line pt-3">{traces.length === 0 ? <p className="font-mono text-[10px] text-stone">Aucun appel pour l’instant.</p> : traces.slice().reverse().map((trace, index) => <div key={`${trace.agent}-${index}`} className="grid gap-1 border-b border-line/70 py-2 font-mono text-[10px] sm:grid-cols-[1fr_.8fr_2fr]"><span className="font-bold">{trace.agent}</span><span className="text-sage">{trace.latency} ms · {trace.status}</span><span className="truncate text-stone">{trace.payload}</span></div>)}</div></motion.div>}</AnimatePresence></div></section>;
}
