"""POST /api/climate-agent/run (mounted by rag_service/main.py)."""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from climate_agent.agent import ClimateRequest, ClimateResponse, run_agent

logger = logging.getLogger("solvarisk.climate_agent")
router = APIRouter(prefix="/api/climate-agent", tags=["climate-agent"])


@router.post("/run", response_model=ClimateResponse)
async def run(request: ClimateRequest) -> ClimateResponse:
    """200 COMPLETED | 200 LLM_UNAVAILABLE (deterministic stress still returned) | 200 DATA_UNAVAILABLE (climate API down) | 422 malformed."""
    try:
        return await run_in_threadpool(run_agent, request)
    except Exception as exc:                   # noqa: BLE001
        logger.exception("climate agent failed")
        raise HTTPException(500, f"Erreur interne de l'agent : {type(exc).__name__}: {str(exc)[:200]}") from exc
