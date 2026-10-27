"""LangGraph orchestration of the two agents.

    START -> data_quality --(status != BLOCKED)--> risk_analyst --> END
                         \\--(status == BLOCKED)--> blocked_alert --> END     (no LLM call, no diagnosis of unreliable data)
"""
from typing import Literal

from langgraph.graph import END, START, StateGraph

from pipeline.agents.data_quality_agent import data_quality_node
from pipeline.agents.risk_analyst_agent import blocked_alert_node, risk_analyst_node
from pipeline.schemas import AgentState, PipelineRequest, PipelineResponse


def route_after_quality(state: AgentState) -> Literal["risk_analyst", "blocked_alert", "__end__"]:
    """Conditional edge: the data quality gate decides whether the analysis may run.
    Without solvency figures (audit before the SCR calculation) a clean audit simply ends the run."""
    if state["data_quality"].status == "BLOCKED":
        return "blocked_alert"
    return "risk_analyst" if state.get("solvency") is not None else END


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("data_quality", data_quality_node)
    builder.add_node("risk_analyst", risk_analyst_node)
    builder.add_node("blocked_alert", blocked_alert_node)
    builder.add_edge(START, "data_quality")
    builder.add_conditional_edges("data_quality", route_after_quality, {"risk_analyst": "risk_analyst", "blocked_alert": "blocked_alert", END: END})
    builder.add_edge("risk_analyst", END)
    builder.add_edge("blocked_alert", END)
    return builder.compile(name="solvarisk_pipeline")


solvarisk_pipeline = build_graph()


async def run_pipeline(request: PipelineRequest) -> PipelineResponse:
    """Runs the compiled graph and shapes its final state into the API response."""
    initial: AgentState = {"portfolio": request.portfolio}
    if request.solvency is not None:
        initial["solvency"] = request.solvency
    final: AgentState = await solvarisk_pipeline.ainvoke(initial)
    return PipelineResponse(
        pipeline_status=final.get("pipeline_status", "AUDITED"),
        data_quality=final["data_quality"],
        diagnosis=final.get("diagnosis"),
        diagnosis_source=final.get("diagnosis_source"),
        guardrail_notes=final.get("guardrail_notes", []),
        errors=final.get("errors", []),
        model=final.get("model_used"),
    )
