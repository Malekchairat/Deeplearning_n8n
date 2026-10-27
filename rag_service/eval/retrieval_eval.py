"""Retrieval evaluation of the Solva RAG: 3 chunking strategies x 4 retrieval modes on 13 test queries + 2 out-of-corpus queries.

Gold standard = the page(s) of the course PDF that contain the answer (read by hand), so it does not depend on how a strategy cuts the text.
A retrieved chunk is relevant if it comes from the right document and its page range intersects the gold pages.
Raw queries are used (no LLM query rewriting): the evaluation needs no API quota and isolates retrieval.

Run (from rag_service/):  python -m eval.retrieval_eval        -> eval/results.json and eval/retrieval_evaluation.md
"""
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import torch
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import rag_core as rc  # noqa: E402

P1, FORM = "1.Pilier1", "Formation"
# (query, document prefix, gold pages)
QUERIES = [
    ("Que représente le SCR et à quel niveau de confiance est-il calibré ?", P1, {14}),
    ("Quelles sont les bornes du MCR par rapport au SCR ?", P1, {14}),
    ("Comment se calcule la marge de risque ?", P1, {13}),
    ("Quelle est la formule du SCR en formule standard ?", P1, {28}),
    ("Qu'est-ce que l'ORSA et à quelle fréquence doit-il être réalisé ?", P1, {54, 55}),
    ("Que sont les ajustements pour la capacité d'absorption des pertes par les provisions techniques et les impôts différés ?", P1, {28, 29}),
    ("Quels sont les trois piliers de Solvabilité 2 ?", P1, {7, 8}),
    ("Comment les modules de risque sont-ils agrégés pour obtenir le SCR de base ?", P1, {27, 32}),
    ("Qu'est-ce que le best estimate des provisions techniques ?", P1, {12}),
    ("De quels sous-modules est composé le risque catastrophe en non-vie ?", P1, {41}),
    ("Comment se calcule le SCR des actifs incorporels ?", P1, {26}),
    ("Que se passe-t-il en cas de non-respect du SCR ou du MCR ?", P1, {14, 30}),
    ("Qu'est-ce que le rapport SFCR et le RSR au titre du Pilier 3 ?", FORM, {88, 89}),
]
OUT_OF_CORPUS = [
    "Quelles sont les limites quantitatives des fonds propres de Tier 2 et de Tier 3 ?",
    "Quel est le taux directeur actuel de la Banque centrale européenne ?",
]
MODES = ["dense", "bm25", "hybrid", "hybrid_rerank"]
TOP_K = 5


# ----------------------------------------------------------------------------- chunking strategies
def chunks_recursive(pages, size, overlap):
    return rc.build_chunks(pages, size=size, overlap=overlap)


def chunks_page(pages):
    return [{"text": p["text"], "page_start": p["page"], "page_end": p["page"]} for p in pages]


STRATEGIES = {
    "A. lignes + phrases, 1100/200 (production)": lambda pages: chunks_recursive(pages, 1100, 200),
    "B. lignes + phrases, 400/80 (petits chunks)": lambda pages: chunks_recursive(pages, 400, 80),
    "C. une page = un chunk (structure du document)": chunks_page,
}


# ----------------------------------------------------------------------------- in-memory index
class Index:
    def __init__(self, chunks):
        self.chunks = chunks
        self._memo = {}
        self.texts = [f"{c['title']}\n{c['text']}" for c in chunks]
        embed_in = [f"{c['title']} (p.{c['page_start']}-{c['page_end']})\n{c['text']}" for c in chunks]
        self.emb = rc.embed_passages(embed_in)
        self.bm25 = BM25Okapi([rc.tokenize(t) for t in self.texts])

    def dense(self, q, n):
        return list(np.argsort(-(self.emb @ rc.embed_query(q)))[:n])

    def sparse(self, q, n):
        s = self.bm25.get_scores(rc.tokenize(q))
        return [int(i) for i in np.argsort(-s)[:n] if s[i] > 0]

    def retrieve(self, q, mode, k=TOP_K):
        key = (q, mode, k)
        if key not in self._memo:
            self._memo[key] = self._retrieve(q, mode, k)
        return self._memo[key]

    def _retrieve(self, q, mode, k):
        n = rc.CFG.n_candidates
        lists = []
        if mode in ("dense", "hybrid", "hybrid_rerank"):
            lists.append(self.dense(q, n))
        if mode in ("bm25", "hybrid", "hybrid_rerank"):
            lists.append(self.sparse(q, n))
        fused = rc.rrf(lists)
        ranked = sorted(fused, key=fused.get, reverse=True)
        scores = {i: fused[i] for i in ranked}
        if mode == "hybrid_rerank":
            cand = ranked[:rc.CFG.n_fused]
            s = np.asarray(rc.RERANKER.predict([(q, self.texts[i]) for i in cand], batch_size=16), dtype=float)
            if s.min() < 0 or s.max() > 1:
                s = 1 / (1 + np.exp(-s))
            scores = dict(zip(cand, s))
            ranked = [cand[i] for i in np.argsort(-s)]
        return [(int(i), float(scores[i])) for i in ranked[:k]]


def relevant(chunk, doc_prefix, pages):
    return chunk["source"].startswith(doc_prefix) and any(chunk["page_start"] <= p <= chunk["page_end"] for p in pages)


def evaluate(index):
    table = {}
    for mode in MODES:
        ranks = []
        for q, doc, pages in QUERIES:
            hits = index.retrieve(q, mode)
            ranks.append(next((r for r, (i, _s) in enumerate(hits, 1) if relevant(index.chunks[i], doc, pages)), None))
        table[mode] = {"hit@1": sum(r == 1 for r in ranks) / len(ranks), "hit@3": sum(r is not None and r <= 3 for r in ranks) / len(ranks),
                       "hit@5": sum(r is not None for r in ranks) / len(ranks), "mrr": sum(1 / r for r in ranks if r) / len(ranks), "ranks": ranks}
    return table


def main():
    torch.set_num_threads(8)
    t0 = time.time()
    rc.EMB = SentenceTransformer(rc.CFG.embed_model)
    rc.EMB.max_seq_length = 512
    rc.IS_E5 = True
    rc.RERANKER = CrossEncoder(rc.CFG.rerank_model, max_length=512)
    docs = []
    for path in sorted(Path(rc.CFG.docs_dir).glob("*.pdf")):
        docs.append((rc.normalize_name(path.name), rc.doc_title(rc.normalize_name(path.name)), rc.load_pdf(path)))
    results = {"corpus": {name: {"pages": len(pages), "chars": sum(len(p["text"]) for p in pages)} for name, _t, pages in docs}, "strategies": {}}
    for label, cutter in STRATEGIES.items():
        chunks = [dict(c, source=name, title=title) for name, title, pages in docs for c in cutter(pages)]
        index = Index(chunks)
        sizes = [len(c["text"]) for c in chunks]
        entry = {"chunks": len(chunks), "mean_chars": round(statistics.mean(sizes)), "max_chars": max(sizes), "modes": evaluate(index)}
        if label.startswith("A."):          # reranker score of the best hit: in-corpus vs out-of-corpus (justifies the 0.05 threshold)
            entry["top1_scores_in_corpus"] = [round(index.retrieve(q, "hybrid_rerank")[0][1], 3) for q, _d, _p in QUERIES]
            entry["top1_scores_out_of_corpus"] = {q: round(index.retrieve(q, "hybrid_rerank")[0][1], 3) for q in OUT_OF_CORPUS}
            entry["per_query_ranks"] = {q: entry["modes"]["hybrid_rerank"]["ranks"][i] for i, (q, _d, _p) in enumerate(QUERIES)}
        results["strategies"][label] = entry
        print(label, entry["chunks"], "chunks", {m: round(v["mrr"], 3) for m, v in entry["modes"].items()}, f"{time.time() - t0:.0f}s", flush=True)
    (HERE / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
