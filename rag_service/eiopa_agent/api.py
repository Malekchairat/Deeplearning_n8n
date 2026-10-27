"""POST /api/eiopa-agent/run and GET /api/eiopa-agent/files/{name} (mounted by rag_service/main.py)."""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from eiopa_agent import excel_qrt
from eiopa_agent.agent import AgentRequest, AgentResponse, run_agent

logger = logging.getLogger("solvarisk.eiopa_agent")
router = APIRouter(prefix="/api/eiopa-agent", tags=["eiopa-agent"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/run", response_model=AgentResponse)
async def run(request: AgentRequest) -> AgentResponse:
    """Runs the agent. 200 COMPLETED | 200 LLM_UNAVAILABLE (deterministic eligibility still returned) | 422 malformed request."""
    try:
        return await run_in_threadpool(run_agent, request)
    except Exception as exc:                   # noqa: BLE001
        logger.exception("eiopa agent failed")
        raise HTTPException(500, f"Erreur interne de l'agent : {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/files/{name}")
async def download(name: str) -> FileResponse:
    """Serves a generated workbook. Only names this service created are accepted (no path traversal)."""
    path = (excel_qrt.GENERATED_DIR / name).resolve()
    if not excel_qrt.SAFE_NAME.match(name) or path.parent != excel_qrt.GENERATED_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(path, media_type=XLSX, filename=name)
