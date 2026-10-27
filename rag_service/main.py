"""HTTP wrapper around the notebook RAG pipeline. Run: uvicorn main:app --port 8001"""
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import rag_core  # noqa: E402


@asynccontextmanager
async def lifespan(_: FastAPI):
    rag_core.init()
    yield


app = FastAPI(title="Solva RAG service", lifespan=lifespan)

# Next.js (dev servers on 3000/3001) may call the service straight from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# PDF -> QRT extraction (tables, Gemini vision fallback): the PDF counterpart of the Excel import
try:
    from extraction.api import router as extraction_router
    app.include_router(extraction_router)
except ImportError as exc:  # pragma: no cover
    print(f"[extraction] disabled, dependency missing: {exc}")

# Expert EIOPA & QRT agent: tools = RAG search, tier-limit computation, Excel QRT generation
try:
    from eiopa_agent.api import router as eiopa_router
    app.include_router(eiopa_router)
except ImportError as exc:  # pragma: no cover
    print(f"[eiopa_agent] disabled, dependency missing: {exc}")

# Climate & ESG Stress Tester: tools = portfolio database, real climate API, flood stress computation
try:
    from climate_agent.api import router as climate_router
    app.include_router(climate_router)
except ImportError as exc:  # pragma: no cover
    print(f"[climate_agent] disabled, dependency missing: {exc}")

# Multi-agent pipeline (LangGraph): data quality gate -> Gemini risk analyst. Optional: a missing dependency must not stop the RAG.
try:
    from pipeline.api import router as pipeline_router
    app.include_router(pipeline_router)
except ImportError as exc:  # pragma: no cover
    print(f"[pipeline] disabled, dependency missing: {exc} (pip install -r requirements.txt)")


class AskRequest(BaseModel):
    # same field names the Next.js copilot already sends to n8n
    chatInput: str
    sessionId: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "chunks": rag_core.collection.count()}


@app.post("/ask")
async def ask(req: AskRequest):
    question = req.chatInput.strip()
    if not question:
        raise HTTPException(400, "chatInput is empty")
    try:
        r = await run_in_threadpool(rag_core.ask, question, req.sessionId)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {
        "answer": r["answer"],
        "sources": r["sources"],
        "queries": r["queries"],
        "source": ", ".join(r["sources"]),
    }


class ReportRequest(BaseModel):
    scrData: dict                      # ratio, scr, mcr, eligibleFunds, modules, details?, warnings?
    analystData: dict | None = None    # status, severity, summary, findings, recommended_action


@app.post("/report")
async def report(req: ReportRequest):
    # imported here so a missing PDF dependency cannot stop the RAG endpoints from starting
    from report_generator import build_report_pdf
    try:
        pdf = await run_in_threadpool(build_report_pdf, req.scrData, req.analystData)
    except Exception as e:
        raise HTTPException(500, f"PDF generation failed: {e}")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="Rapport_SCR.pdf"'})


@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in (".pdf", ".txt", ".md"):
        raise HTTPException(400, "Only .pdf, .txt and .md files are supported")
    dest = Path(rag_core.CFG.docs_dir) / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    report = await run_in_threadpool(rag_core.ingest_folder)
    return {"report": report}
