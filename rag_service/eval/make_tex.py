"""Writes docs/rapport/rag_tables.tex from eval/results.json (the report's numbers are never typed by hand). Run: python -m eval.make_tex"""
import json
import re
from pathlib import Path

from eval.retrieval_eval import MODES

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs" / "rapport" / "rag_tables.tex"
res = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
MODE_FR = {"dense": "dense", "bm25": "BM25", "hybrid": "hybride (RRF)", "hybrid_rerank": "hybride + reranker"}
pct = lambda v: f"{100 * v:.0f}\\,\\%"          # noqa: E731

rows = []
for label, entry in res["strategies"].items():
    short = label.split(" (")[0].replace("lignes + phrases, ", "").replace("une page = un chunk", "page").replace("&", "\\&")
    rows.append(f"\\multicolumn{{6}}{{@{{}}l}}{{\\textbf{{{short}}} --- {entry['chunks']} chunks, {entry['mean_chars']} car. en moyenne}}\\\\")
    for m in MODES:
        d = entry["modes"][m]
        rows.append(f"\\quad {MODE_FR[m]} & {pct(d['hit@1'])} & {pct(d['hit@3'])} & {pct(d['hit@5'])} & {d['mrr']:.2f} & \\\\")
    rows.append("\\addlinespace[2pt]")
A = next(k for k in res["strategies"] if k.startswith("A."))
inn, out = res["strategies"][A]["top1_scores_in_corpus"], res["strategies"][A]["top1_scores_out_of_corpus"]
ooc = ", ".join(f"{s:.3f}".replace(".", ",") for s in out.values())
tex = ("\\begin{table}[h]\\centering\\small\n\\begin{tabular}{@{}lrrrrl@{}}\\toprule\n\\textbf{D\\'ecoupage / mode} & hit@1 & hit@3 & hit@5 & MRR & \\\\\\midrule\n"
       + "\n".join(rows) + "\n\\bottomrule\\end{tabular}\n"
       "\\caption{Qualit\\'e du retrieval (13 questions du corpus, top-5, requ\\^etes brutes).}\n\\end{table}\n"
       f"\\textbf{{Questions hors corpus.}} Meilleur score du reranker~: {min(inn):.3f} \\`a {max(inn):.3f} sur les questions du corpus (m\\'ediane {sorted(inn)[len(inn) // 2]:.3f}), "
       f"contre {ooc} sur les deux questions hors corpus (limites de Tier~; taux directeur de la BCE), pour un seuil de refus de 0,05.\n")
tex = re.sub(r"(?<=\d)\.(?=\d)", ",", tex)          # French decimal commas
OUT.write_text(tex, encoding="utf-8")
print(tex)
