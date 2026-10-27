"""HTTP surface of the pipeline: POST /api/pipeline/audit-and-risk (mounted by rag_service/main.py)."""
import logging

from fastapi import APIRouter, HTTPException

from pipeline.graph import run_pipeline
from pipeline.schemas import PipelineRequest, PipelineResponse

logger = logging.getLogger("solvarisk.pipeline")
router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/audit-and-risk", response_model=PipelineResponse)
async def audit_and_risk(request: PipelineRequest) -> PipelineResponse:
    """Audits the portfolio data, then (if not blocked) produces the risk diagnosis.

    * 200 + pipeline_status COMPLETED       : audit passed/warned, diagnosis from the LLM
    * 200 + pipeline_status BLOCKED         : critical data anomalies, deterministic alert, LLM not called
    * 200 + pipeline_status ANALYSIS_FAILED : audit done but the LLM could not answer (details in `errors`)
    * 422 malformed request (FastAPI)       * 500 unexpected failure inside the graph
    """
    try:
        return await run_pipeline(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("pipeline failed")
        raise HTTPException(status_code=500, detail=f"Erreur interne du pipeline : {type(exc).__name__}: {str(exc)[:300]}") from exc
