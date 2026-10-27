"""POST /api/extract/qrt-pdf : the PDF twin of the Excel import (mounted by rag_service/main.py)."""
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from extraction.qrt_pdf import ExtractionError, QrtExtraction, extract_qrt_from_pdf

logger = logging.getLogger("solvarisk.extraction")
router = APIRouter(prefix="/api/extract", tags=["extraction"])
MAX_BYTES = 15 * 1024 * 1024


@router.post("/qrt-pdf", response_model=QrtExtraction)
async def qrt_pdf(file: UploadFile = File(...)) -> QrtExtraction:
    """Returns the QRT lines of a PDF as `sheets`, in the same shape the Excel parser produces.

    400 not a (readable) PDF | 413 too big / too many pages | 422 no QRT line found | 502/503 the vision model is unavailable
    """
    name = (file.filename or "").lower()
    if not name.endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(400, "Seuls les fichiers PDF sont acceptés ici (l'Excel est lu directement par l'application).")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"PDF trop volumineux (maximum {MAX_BYTES // (1024 * 1024)} Mo).")
    try:
        return await run_in_threadpool(extract_qrt_from_pdf, data)
    except ExtractionError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except Exception as exc:                   # noqa: BLE001
        logger.exception("pdf extraction failed")
        raise HTTPException(500, f"Erreur interne d'extraction : {type(exc).__name__}: {str(exc)[:200]}") from exc
