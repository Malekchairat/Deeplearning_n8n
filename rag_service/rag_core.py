"""RAG Solvabilité 2 — pipeline ported from RAG_Solva2_v2.ipynb (Colab cells removed).

PDF -> cleaning (PyMuPDF + NFKC) -> page-aware chunks -> multilingual embeddings -> ChromaDB (cosine)
-> hybrid search (dense + BM25, RRF fusion) -> cross-encoder reranker -> Gemini with [n] citations.
"""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import chromadb
import fitz  # PyMuPDF
import numpy as np
from google import genai
from google.genai import types
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

HERE = Path(__file__).resolve().parent


@dataclass
class Config:
    docs_dir: str = os.environ.get("RAG_DOCS_DIR", str(HERE / "documents"))
    db_dir: str = os.environ.get("RAG_DB_DIR", str(HERE / "chroma_db_v2"))
    embed_model: str = os.environ.get("RAG_EMBED_MODEL", "intfloat/multilingual-e5-base")
    rerank_model: str = os.environ.get("RAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    llm_model: str = os.environ.get("RAG_LLM_MODEL", "gemini-3.5-flash-lite")
    chunk_size: int = 1100
    chunk_overlap: int = 200
    min_page_chars: int = 40
    n_candidates: int = 30
    n_fused: int = 20
    top_k: int = 5
    min_rerank_score: float = 0.05
    use_query_rewrite: bool = True
    max_context_chars: int = 12000


CFG = Config()
NOT_FOUND = "Information non trouvée dans les documents."

# ----------------------------------------------------------------------------- text cleaning / loading
BULLETS = "•▪●■⬤⚫‣⁃"


def normalize_name(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def clean_text(t: str) -> str:
    # NFKC turns "mathematical" letters from formulas (𝑩𝒆𝒔𝒕 𝑬𝒔𝒕𝒊𝒎𝒂𝒕𝒆 -> Best Estimate) into plain text
    t = unicodedata.normalize("NFKC", t)
    t = t.replace(" ", " ")
    t = re.sub(f"[{BULLETS}]", "-", t)
    t = re.sub(r"[-]", "", t)
    t = re.sub(r"(?<=\w)-\n(?=[a-zà-ÿ])", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def load_pdf(path):
    pages = []
    with fitz.open(path) as pdf:
        for i, page in enumerate(pdf, start=1):
            txt = clean_text(page.get_text("text", sort=True))
            if len(txt) >= CFG.min_page_chars:
                pages.append({"page": i, "text": txt})
    return pages


def load_document(path: Path):
    if path.suffix.lower() == ".pdf":
        return load_pdf(path)
    return [{"page": 1, "text": clean_text(path.read_text(encoding="utf-8", errors="ignore"))}]


def doc_title(source: str) -> str:
    return re.sub(r"^\d+\.", "", Path(source).stem).strip()


# ----------------------------------------------------------------------------- chunking
def split_sentences(text, max_len):
    sents = re.split(r"(?<=[.!?;:])\s+", text)
    out, cur = [], ""
    for s in sents:
        s = s.strip()
        if not s:
            continue
        while len(s) > max_len:
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:max_len])
            s = s[max_len:]
        if cur and len(cur) + 1 + len(s) > max_len:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def page_units(pages, size):
    units = []
    for p in pages:
        for line in p["text"].split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) > size:
                units.extend((p["page"], piece) for piece in split_sentences(line, size))
            else:
                units.append((p["page"], line))
    return units


def build_chunks(pages, size=None, overlap=None):
    size = size or CFG.chunk_size
    overlap = CFG.chunk_overlap if overlap is None else overlap
    units = page_units(pages, size)
    chunks, cur, cur_len = [], [], 0

    def flush(keep_overlap):
        nonlocal cur, cur_len
        chunks.append(cur)
        keep, length = [], 0
        if keep_overlap:
            for u in reversed(cur):
                if length + len(u[1]) + 1 > overlap:
                    break
                keep.insert(0, u)
                length += len(u[1]) + 1
        cur, cur_len = keep, length

    for page, txt in units:
        too_long = cur_len + len(txt) + 1 > size
        page_break = bool(cur) and page != cur[-1][0] and cur_len >= size * 0.5
        if cur and (too_long or page_break):
            flush(keep_overlap=not page_break)
        cur.append((page, txt))
        cur_len += len(txt) + 1
    if cur:
        chunks.append(cur)

    result = []
    for c in chunks:
        text = "\n".join(t for _, t in c)
        if len(text) >= 30:
            result.append({"text": text, "page_start": c[0][0], "page_end": c[-1][0]})
    return result


# ----------------------------------------------------------------------------- models / store
EMB = None
IS_E5 = False
RERANKER = None
GEMINI = None
collection = None
STATE = {}
HISTORIES = {}      # session_id -> [(question, answer), ...]
_LOCK = threading.RLock()


def init():
    """Load models, open ChromaDB and index the documents folder. Call once at startup."""
    global EMB, IS_E5, RERANKER, GEMINI, collection
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (put it in rag_service/.env).")
    GEMINI = genai.Client(api_key=key)

    EMB = SentenceTransformer(CFG.embed_model)
    EMB.max_seq_length = 512
    IS_E5 = "e5" in CFG.embed_model.lower()   # E5 models need "query:" / "passage:" prefixes
    try:
        RERANKER = CrossEncoder(CFG.rerank_model, max_length=512)
    except Exception as e:
        print("Reranker unavailable, continuing without:", e)
        RERANKER = None

    client = chromadb.PersistentClient(path=CFG.db_dir)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", CFG.embed_model).strip("_")[:40]
    name = f"solva_{slug}"   # one index per embedding model
    try:
        collection = client.get_or_create_collection(name, configuration={"hnsw": {"space": "cosine"}})
    except Exception:
        collection = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})

    Path(CFG.docs_dir).mkdir(parents=True, exist_ok=True)
    ingest_folder()


def embed_passages(texts):
    texts = [("passage: " + t) if IS_E5 else t for t in texts]
    return EMB.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)


def embed_query(q):
    return EMB.encode(("query: " + q) if IS_E5 else q, normalize_embeddings=True)


def llm_generate(prompt, json_mode=False, temperature=0.0):
    kw = {"temperature": temperature}
    if json_mode:
        kw["response_mime_type"] = "application/json"
    config = types.GenerateContentConfig(**kw)
    last = None
    for attempt in range(5):
        try:
            r = GEMINI.models.generate_content(model=CFG.llm_model, contents=prompt, config=config)
            return (r.text or "").strip()
        except Exception as e:
            last = e
            if any(s in str(e) for s in ("404", "NOT_FOUND", "API_KEY", "PERMISSION", "INVALID_ARGUMENT")):
                raise
            time.sleep(min(2 ** attempt, 30))   # 429 / 503: retry with growing delay
    raise RuntimeError(f"Gemini failed after 5 attempts: {last}")


# ----------------------------------------------------------------------------- incremental indexing + BM25
STOP = set("le la les l un une des du de d et en au aux a est sont que qui quoi quel quelle quels quelles "
           "pour par sur dans avec ce ces cette il elle ils on se sa son ses leur leurs ou ne pas plus comme "
           "sous entre s c qu n y".split())


def fold(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def tokenize(t: str):
    out = []
    for w in re.findall(r"[a-z0-9]+", fold(t)):
        if w in STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith(("s", "x")):
            w = w[:-1]
        out.append(w)
    return out


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def is_indexed(source: str, h: str) -> bool:
    res = collection.get(where={"source": source}, limit=1, include=["metadatas"])
    return bool(res["ids"]) and res["metadatas"][0].get("file_hash") == h


def refresh_bm25():
    data = collection.get(include=["documents", "metadatas"])
    STATE["ids"], STATE["docs"], STATE["metas"] = data["ids"], data["documents"], data["metadatas"]
    STATE["pos"] = {cid: i for i, cid in enumerate(STATE["ids"])}
    corpus = [tokenize(m.get("title", "") + " " + d) for d, m in zip(STATE["docs"], STATE["metas"])]
    STATE["bm25"] = BM25Okapi(corpus) if corpus else None
    print(f"BM25 index ready: {len(corpus)} chunks")


def ingest_folder(folder=None, force=False):
    """Index new or modified files only. Returns a per-file report."""
    folder = Path(folder or CFG.docs_dir)
    report = []
    with _LOCK:
        for path in sorted(p for p in folder.glob("*") if p.suffix.lower() in (".pdf", ".txt", ".md")):
            source = normalize_name(path.name)
            h = file_hash(path)
            if not force and is_indexed(source, h):
                report.append({"source": source, "status": "already-indexed"})
                continue
            pages = load_document(path)
            if not pages:
                report.append({"source": source, "status": "no-text (scanned PDF? needs OCR)"})
                continue
            chunks = build_chunks(pages)
            title = doc_title(source)
            prefix = hashlib.md5(source.encode()).hexdigest()[:8]
            collection.delete(where={"source": source})
            ids, docs, metas, to_embed = [], [], [], []
            for i, c in enumerate(chunks):
                ids.append(f"{prefix}_{i}")
                docs.append(c["text"])
                metas.append({"source": source, "title": title, "chunk_id": i, "file_hash": h,
                              "page_start": c["page_start"], "page_end": c["page_end"]})
                to_embed.append(f"{title} (p.{c['page_start']}-{c['page_end']})\n{c['text']}")
            collection.add(ids=ids, documents=docs, embeddings=embed_passages(to_embed).tolist(), metadatas=metas)
            report.append({"source": source, "status": "indexed", "chunks": len(chunks)})
        refresh_bm25()
    return report


# ----------------------------------------------------------------------------- retrieval
def dense_search(query, n):
    total = collection.count()
    if total == 0:
        return []
    r = collection.query(query_embeddings=[embed_query(query).tolist()], n_results=min(n, total),
                         include=["distances"])
    return r["ids"][0]


def bm25_search(query, n):
    if STATE.get("bm25") is None:
        return []
    scores = STATE["bm25"].get_scores(tokenize(query))
    order = np.argsort(-scores)[:n]
    return [STATE["ids"][i] for i in order if scores[i] > 0]


def rrf(rank_lists, k=60):
    fused = {}
    for ranks in rank_lists:
        for r, cid in enumerate(ranks):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + r + 1)
    return fused


def rerank(query, ids):
    pairs = []
    for cid in ids:
        j = STATE["pos"][cid]
        pairs.append((query, f"{STATE['metas'][j].get('title', '')}\n{STATE['docs'][j]}"))
    scores = np.asarray(RERANKER.predict(pairs, batch_size=16), dtype=float)
    if scores.min() < 0 or scores.max() > 1:
        scores = 1 / (1 + np.exp(-scores))
    return scores


def retrieve(queries, mode="hybrid_rerank", top_k=None, apply_threshold=True):
    """mode: dense | bm25 | hybrid | hybrid_rerank"""
    top_k = top_k or CFG.top_k
    if isinstance(queries, str):
        queries = [queries]
    n = CFG.n_candidates
    rank_lists = []
    for q in queries:
        if mode in ("dense", "hybrid", "hybrid_rerank"):
            rank_lists.append(dense_search(q, n))
        if mode in ("bm25", "hybrid", "hybrid_rerank"):
            rank_lists.append(bm25_search(q, n))
    fused = rrf(rank_lists)
    ranked = sorted(fused, key=fused.get, reverse=True)
    score_map = fused
    if mode == "hybrid_rerank" and RERANKER is not None and ranked:
        cand = ranked[:CFG.n_fused]
        scores = rerank(queries[0], cand)
        score_map = dict(zip(cand, scores))
        ranked = [cand[i] for i in np.argsort(-scores)]
        if apply_threshold:
            ranked = [c for c in ranked if score_map[c] >= CFG.min_rerank_score]
    hits = []
    for cid in ranked[:top_k]:
        j = STATE["pos"][cid]
        hits.append({"id": cid, "text": STATE["docs"][j], "meta": STATE["metas"][j], "score": float(score_map[cid])})
    return hits


# ----------------------------------------------------------------------------- query rewrite + generation
REWRITE_PROMPT = """Tu aides un moteur de recherche documentaire sur Solvabilité 2 (assurance, actuariat, réassurance).
HISTORIQUE :
{history}
QUESTION : {q}

Réponds en JSON : {{"standalone": "...", "variants": ["...", "..."]}}
- standalone : la question reformulée de façon autonome en français (résous les pronoms et ellipses avec l'historique).
- variants : 2 reformulations courtes pour la recherche, avec un vocabulaire différent, acronymes développés
  (SCR = capital de solvabilité requis, MCR, BE = Best Estimate, PSAP, ORSA, FP = fonds propres...) et termes anglais utiles."""

ANSWER_PROMPT = """Tu es un assistant pédagogique expert en Solvabilité 2, actuariat et réassurance (niveau Master).

RÈGLES
1. Réponds UNIQUEMENT à partir des EXTRAITS ci-dessous, sans connaissances extérieures.
2. Cite chaque affirmation avec le numéro de l'extrait entre crochets, ex. [1] ou [2][3].
3. Si les extraits ne couvrent la question que partiellement, réponds sur la partie couverte et dis précisément ce qui manque.
4. Si aucun extrait ne répond, réponds exactement : "Information non trouvée dans les documents."
5. Réponse claire et structurée, en français ; garde les sigles, formules et chiffres tels qu'ils apparaissent.

EXTRAITS
{context}

{history_block}QUESTION : {question}
RÉPONSE :"""


def rewrite_query(question, history):
    if not CFG.use_query_rewrite:
        return [question]
    try:
        h = "\n".join(f"Q: {a}\nR: {b[:300]}" for a, b in history[-3:]) or "(aucun)"
        data = json.loads(llm_generate(REWRITE_PROMPT.format(history=h, q=question), json_mode=True))
        cands = [data.get("standalone") or question] + list(data.get("variants", []))[:2]
        cleaned = [str(x).strip() for x in cands if str(x).strip()]
        return list(dict.fromkeys(cleaned)) or [question]
    except Exception as e:
        print("(query rewrite skipped:", e, ")")
        return [question]


def fmt_source(m):
    p = m["page_start"] if m["page_start"] == m["page_end"] else f"{m['page_start']}-{m['page_end']}"
    return f"{m['source']} (p. {p})"


def build_context(hits):
    blocks, total = [], 0
    for i, h in enumerate(hits, 1):
        b = f"[{i}] {fmt_source(h['meta'])}\n{h['text']}"
        if total + len(b) > CFG.max_context_chars and blocks:
            break
        blocks.append(b)
        total += len(b)
    return "\n\n---\n\n".join(blocks), len(blocks)


def rag(question, history=None, mode="hybrid_rerank"):
    history = history or []
    queries = rewrite_query(question, history)
    hits = retrieve(queries, mode=mode)
    if not hits:   # nothing relevant: skip the LLM entirely
        return {"answer": NOT_FOUND, "hits": [], "sources": [], "queries": queries}
    context, n_used = build_context(hits)
    hits = hits[:n_used]
    hist_block = ""
    if history:
        hist_block = "HISTORIQUE DE CONVERSATION :\n" + "\n".join(
            f"Q: {a}\nR: {b[:400]}" for a, b in history[-2:]) + "\n\n"
    answer = llm_generate(ANSWER_PROMPT.format(context=context, history_block=hist_block, question=queries[0]))
    answer = re.sub(r"^\s*R[ÉE]PONSE\s*:\s*", "", answer)   # Gemini sometimes echoes the prompt's last line
    sources = list(dict.fromkeys(fmt_source(h["meta"]) for h in hits))
    return {"answer": answer, "hits": hits, "sources": sources, "queries": queries}


def ask(question, session_id=None):
    """Session-aware entry point used by the HTTP API."""
    history = HISTORIES.setdefault(session_id, []) if session_id else []
    r = rag(question, history)
    if session_id:
        history.append((question, r["answer"]))
        del history[:-10]
    return r
