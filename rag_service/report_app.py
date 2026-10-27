"""Standalone PDF service (no RAG models): same report_generator.py as rag_service/main.py, run in Docker for n8n.

n8n -> POST http://report-pdf:8002/report  { scrData, analystData }  ->  application/pdf
"""
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel

from report_generator import build_report_pdf

app = FastAPI(title="Solva report service")


class ReportRequest(BaseModel):
    scrData: dict
    analystData: dict | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/report")
async def report(req: ReportRequest):
    try:
        pdf = await run_in_threadpool(build_report_pdf, req.scrData, req.analystData)
    except Exception as e:
        raise HTTPException(500, f"PDF generation failed: {e}")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="Rapport_SCR.pdf"'})
