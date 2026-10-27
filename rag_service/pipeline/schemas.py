"""Data contracts of the SolvaRisk multi-agent pipeline.

Three groups:
  * inputs      - PortfolioPayload (raw positions to audit) and SolvencyMetrics (SCR, MCR, own funds...)
  * agent 1     - Anomaly / DataQualityReport (deterministic audit, Art. 82 Directive 2009/138/EC)
  * agent 2     - RiskDiagnosisReport (the LLM structured output) and the shared LangGraph AgentState
"""
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ----------------------------------------------------------------------------- inputs


class SourceMetadata(BaseModel):
    """Where the portfolio extract comes from (traceability criterion)."""
    model_config = ConfigDict(extra="allow")

    system: str | None = Field(None, description="Source system (policy admin system, ERP, Excel extract...)")
    extraction_date: str | None = Field(None, description="Extraction date of the data")
    reference: str | None = Field(None, description="Batch id, file name or checksum")
    version: str | None = Field(None, description="Version of the extract / data model")
    owner: str | None = Field(None, description="Data owner or preparer")


class PortfolioPayload(BaseModel):
    """Raw portfolio. Positions stay untyped dicts on purpose: the audit must see what is really missing or malformed,
    so a bad value must become an Anomaly, never a request-validation error."""
    model_config = ConfigDict(extra="allow")

    valuation_date: str | None = Field(None, description="Valuation date (ISO yyyy-mm-dd or dd/mm/yyyy)")
    source: SourceMetadata | None = None
    positions: list[dict[str, Any]] = Field(default_factory=list)
    document_kind: Literal["auto", "summary"] = Field(
        "auto", description="auto: positions or QRT tables (detected); summary: one record of published figures (scr, eligible_funds, mcr, ratios...)")


class RiskModule(BaseModel):
    name: str
    value: float


class SolvencyMetrics(BaseModel):
    """Output of the SCR engine. Accepts the exact JSON the frontend already holds (camelCase `eligibleFunds`...)."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    scr: float = Field(gt=0, description="Solvency Capital Requirement (M EUR)")
    mcr: float = Field(ge=0, description="Minimum Capital Requirement (M EUR)")
    eligible_funds: float = Field(alias="eligibleFunds", description="Eligible own funds covering the SCR (M EUR)")
    ratio: float | None = Field(None, description="Solvency ratio in %. Computed from funds / SCR when absent")
    bscr: float | None = Field(None, description="Basic SCR (M EUR)")
    modules: list[RiskModule] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_derived(self) -> "SolvencyMetrics":
        if self.ratio is None:
            self.ratio = round(self.eligible_funds / self.scr * 100, 2)
        if self.bscr is None and isinstance(self.details.get("bscr"), (int, float)):
            self.bscr = float(self.details["bscr"])
        return self


class PipelineRequest(BaseModel):
    portfolio: PortfolioPayload
    solvency: SolvencyMetrics | None = Field(
        None, description="Omit to run the data quality gate alone (before the SCR calculation); provide it to also get the diagnosis")


# ----------------------------------------------------------------------------- agent 1: data quality


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Criterion(str, Enum):
    COMPLETENESS = "exhaustivite"
    ACCURACY = "exactitude"
    RELEVANCE = "pertinence"
    TRACEABILITY = "tracabilite"


class Anomaly(BaseModel):
    code: str = Field(description="Stable rule code, e.g. EXPOSURE_NEGATIVE")
    criterion: Criterion
    severity: Severity
    message: str
    field: str | None = Field(None, description="Variable concerned")
    row: int | None = Field(None, description="1-based position index; null for portfolio-level anomalies")
    record_id: str | None = None
    value: Any | None = None


class DataQualityReport(BaseModel):
    quality_score: float = Field(ge=0, le=100)
    status: Literal["PASSED", "WARNING", "BLOCKED"]
    is_eligible_for_scr: bool
    rows_audited: int
    anomalies: list[Anomaly] = Field(description="Listed anomalies, most severe first (capped, see anomalies_truncated)")
    anomalies_truncated: int = Field(0, description="Anomalies counted but not listed")
    counts_by_severity: dict[str, int] = Field(default_factory=dict)
    counts_by_code: dict[str, int] = Field(default_factory=dict)


# ----------------------------------------------------------------------------- agent 2: risk diagnosis (LLM output)


class Finding(BaseModel):
    indicator: str = Field(description="Indicator examined, e.g. 'Ratio SCR'")
    evidence: str = Field(description="The exact figure(s) taken from the input, e.g. '148.14 %'")
    interpretation: str = Field(description="What it means for solvency")


class RiskDriver(BaseModel):
    driver: str = Field(description="Risk module or factor, e.g. 'Souscription Non-Vie'")
    weight_pct: float | None = Field(None, description="Share of the sum of modules, copied from the provided indicators")
    comment: str


class MitigationPlan(BaseModel):
    lever: str = Field(description="Reinsurance, securitisation, asset reallocation, de-risking, capital...")
    description: str
    expected_effect: str


class RiskDiagnosisReport(BaseModel):
    """Strict structured output of the Risk Investigation Analyst. Field names `status`, `severity`, `summary`,
    `findings`, `recommended_action` are the ones the existing 'Diagnostic de l'Analyste' panel already reads."""
    status: Literal["normal", "watchlist", "anomaly_detected"]
    severity: Literal["low", "medium", "high"]
    summary: str = Field(description="Executive summary, 3 to 5 sentences, plain text")
    robustness_assessment: str = Field(description="Financial robustness against the 100 % regulatory and 130 % internal thresholds")
    findings: list[Finding] = Field(min_length=1, max_length=8)
    risk_drivers: list[RiskDriver] = Field(max_length=6)
    orsa_recommendations: list[str] = Field(max_length=6)
    mitigation_plans: list[MitigationPlan] = Field(max_length=5)
    recommended_action: str = Field(description="The single most important action to take now")


# ----------------------------------------------------------------------------- LangGraph shared state / API response

PipelineStatus = Literal["COMPLETED", "BLOCKED", "ANALYSIS_FAILED", "AUDITED"]    # AUDITED = quality gate only, nothing to analyse yet


class AgentState(TypedDict, total=False):
    """State flowing through the graph. Nodes return partial dicts that LangGraph merges into it."""
    portfolio: PortfolioPayload
    solvency: SolvencyMetrics
    data_quality: DataQualityReport
    diagnosis: RiskDiagnosisReport
    diagnosis_source: Literal["llm", "alert"]
    model_used: str
    guardrail_notes: list[str]
    errors: list[str]
    pipeline_status: PipelineStatus


class PipelineResponse(BaseModel):
    pipeline_status: PipelineStatus
    data_quality: DataQualityReport
    diagnosis: RiskDiagnosisReport | None = None
    diagnosis_source: Literal["llm", "alert"] | None = None
    guardrail_notes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    model: str | None = Field(None, description="LLM used, when the analyst ran")
