"""Turns eval/results.json into eval/retrieval_evaluation.md (deliverable: short retrieval evaluation). Run: python -m eval.make_report"""
import json
from pathlib import Path

from eval.retrieval_eval import MODES, OUT_OF_CORPUS, QUERIES

HERE = Path(__file__).resolve().parent
res = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
pct = lambda v: f"{100 * v:.0f} %"          # noqa: E731
A = next(k for k in res["strategies"] if k.startswith("A."))

lines = ["# Évaluation du retrieval — SolvaRisk", "",
         "Corpus : " + ", ".join(f"« {n} » ({v['pages']} p., {v['chars']:,} car.)".replace(",", " ") for n, v in res["corpus"].items()) + ".", "",
         f"Protocole : {len(QUERIES)} questions en français dont la réponse est dans le corpus + {len(OUT_OF_CORPUS)} questions hors corpus. "
         "Vérité terrain = page(s) du PDF contenant la réponse (lues à la main). Un passage est pertinent s'il vient du bon document et que ses pages "
         "recoupent les pages attendues. Requêtes brutes (sans réécriture LLM), top-5. Métriques : hit@k = au moins un passage pertinent parmi les k premiers ; MRR.", "",
         "## 1. Comparaison de stratégies de découpage × modes de recherche", ""]
for label, entry in res["strategies"].items():
    lines += [f"### {label}", f"{entry['chunks']} chunks, {entry['mean_chars']} caractères en moyenne (max {entry['max_chars']}).", "",
              "| Mode | hit@1 | hit@3 | hit@5 | MRR |", "|---|---|---|---|---|"]
    lines += [f"| {m} | {pct(entry['modes'][m]['hit@1'])} | {pct(entry['modes'][m]['hit@3'])} | {pct(entry['modes'][m]['hit@5'])} | {entry['modes'][m]['mrr']:.2f} |" for m in MODES]
    lines.append("")
lines += ["## 2. Détail par question (stratégie de production, hybride + reranker)", "", "| Question | Rang du 1er passage pertinent |", "|---|---|"]
lines += [f"| {q} | {r if r else 'non trouvé (top-5)'} |" for q, r in res["strategies"][A]["per_query_ranks"].items()]
inn, out = res["strategies"][A]["top1_scores_in_corpus"], res["strategies"][A]["top1_scores_out_of_corpus"]
lines += ["", "## 3. Questions hors corpus et seuil du reranker", "",
          f"Meilleur score du reranker sur les questions du corpus : min {min(inn):.3f}, médiane {sorted(inn)[len(inn) // 2]:.3f}, max {max(inn):.3f}.", "",
          "| Question hors corpus | Meilleur score reranker |", "|---|---|"]
lines += [f"| {q} | {s:.3f} |" for q, s in out.items()]
lines += ["", f"Seuil de production : 0,05 (`min_rerank_score`). Une question hors corpus dont le meilleur score est inférieur au seuil ne déclenche aucun appel au LLM "
          "et reçoit « Information non trouvée dans les documents. »; au-dessus, le passage est transmis au LLM, dont la règle de refus (prompt P2) doit alors intervenir.", ""]
(HERE / "retrieval_evaluation.md").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
