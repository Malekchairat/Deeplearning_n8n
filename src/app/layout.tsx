import type { Metadata } from "next";
import "katex/dist/katex.min.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "SolvaRisk — laboratoire actuariel",
  description: "Showcase académique Deep Learning & Multi-Agent pour Solvency II.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="fr">
      <body className="bg-paper text-ink antialiased">
        {children}
        <style>{`
          * { box-sizing: border-box; }
          body { margin: 0; min-width: 320px; }
          .katex { font-size: 1em; }
          @media print {
            .no-print { display: none !important; }
            body { background: white; }
            .print-report { display: block !important; }
          }
        `}</style>
      </body>
    </html>
  );
}
