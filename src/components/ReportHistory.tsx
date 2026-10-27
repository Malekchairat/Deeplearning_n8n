"use client";

import { ArrowDown, Clock, FilePdf, Robot } from "@phosphor-icons/react";
import type { GeneratedReport } from "./QrtAndScrView";

export default function ReportHistory({ reports }: { reports: GeneratedReport[] }) {
  return (
    <div className="space-y-6">
      <div className="rounded-3xl border border-line bg-ink p-7 text-white shadow-tactile sm:p-9">
        <div className="flex items-start justify-between gap-5">
          <div><span className="font-mono text-xs font-bold uppercase tracking-[.16em] text-blue-200">REPORT AGENT</span><h2 className="mt-3 text-3xl font-bold tracking-tight sm:text-4xl">Report history</h2><p className="mt-3 max-w-xl text-base leading-7 text-slate-300">Chaque rapport est généré automatiquement après le diagnostic SCR. Retrouvez ici les trois derniers rapports produits et téléchargez-les à tout moment.</p></div>
          <div className="hidden h-14 w-14 items-center justify-center rounded-2xl bg-white/10 sm:flex"><Robot size={30} weight="duotone" /></div>
        </div>
        <div className="mt-7 flex items-center gap-3 text-sm text-slate-300"><span className="h-2.5 w-2.5 rounded-full bg-emerald-400" />Automatic generation enabled</div>
      </div>
      <section className="rounded-3xl border border-line bg-white shadow-tactile">
        <div className="flex items-center justify-between border-b border-line px-6 py-5"><div><h3 className="text-xl font-bold">Latest reports</h3><p className="mt-1 text-sm text-stone">The three most recent generated reports are kept here.</p></div><FilePdf size={25} className="text-clay" /></div>
        {reports.length === 0 ? <div className="flex min-h-64 flex-col items-center justify-center px-6 text-center"><div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-clay"><Clock size={28} /></div><h4 className="mt-4 text-lg font-bold">No reports yet</h4><p className="mt-2 max-w-sm text-sm leading-6 text-stone">Import a QRT file and complete an SCR diagnosis. The reporting agent will add the PDF here automatically.</p></div> : <div className="divide-y divide-line">{reports.slice(0, 3).map((report) => <div key={`${report.createdAt}-${report.url}`} className="flex items-center justify-between gap-4 px-6 py-5"><div className="flex min-w-0 items-center gap-3"><div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-red-50 text-red-600"><FilePdf size={20} weight="fill" /></div><div className="min-w-0"><p className="truncate text-base font-bold">{report.name}</p><p className="mt-1 font-mono text-xs uppercase text-stone">{new Date(report.createdAt).toLocaleString("fr-FR")} · PDF generated</p></div></div><a href={report.url} download={`SolvaRisk-${report.createdAt.slice(0, 10)}.pdf`} className="flex shrink-0 items-center gap-2 rounded-xl bg-ink px-4 py-2.5 text-sm font-bold text-white hover:bg-clay"><ArrowDown size={17} /> Download</a></div>)}</div>}
      </section>
    </div>
  );
}
