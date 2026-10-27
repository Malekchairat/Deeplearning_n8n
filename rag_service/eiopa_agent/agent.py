"""Expert EIOPA & QRT agent: a tool-calling agent (LangChain `create_agent`, LangGraph under the hood).

Loop: the model reads the question, calls search_eiopa_vector_db to confirm the rule, get_own_funds_portfolio to read the data,
check_tier_limits to compute, and generate_excel_qrt to write the file. What is verifiable does not depend on the model:
sources, eligibility and file come from the tools' own state, never from the model's text.
"""
import os
import time
from pathlib import Path
from typing import Any, Callable, Literal

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from eiopa_agent.own_funds import Eligibility, OwnFundsPortfolio
from eiopa_agent.tools import AgentContext, Retriever, build_tools, default_retriever

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

AGENT_MODEL = os.getenv("EIOPA_AGENT_MODEL", "gemini-3.5-flash")
AGENT_FALLBACK_MODEL = os.getenv("EIOPA_AGENT_FALLBACK_MODEL", "gemini-3-flash-preview")
AGENT_LAST_RESORT_MODEL = os.getenv("EIOPA_AGENT_LAST_RESORT_MODEL", "gemini-3.5-flash-lite")   # own quota bucket: still answers when the two above are exhausted
MODELS = list(dict.fromkeys(m for m in (AGENT_MODEL, AGENT_FALLBACK_MODEL, AGENT_LAST_RESORT_MODEL) if m))
KEY_ENV_VARS = ("EIOPA_AGENT_API_KEY", "GEMINI_API_KEY", "INVESTIGATOR_API_KEY", "GOOGLE_API_KEY")
RECURSION_LIMIT = 20

SYSTEM_PROMPT = """Tu es l'agent « Expert EIOPA & QRT » de SolvaRisk (Solvabilité II). Tu vérifies des règles réglementaires sur les fonds propres d'un organisme et, à la demande, tu génères les états QRT correspondants.

OUTILS
- search_eiopa_vector_db : cherche la règle dans la base documentaire.
- get_own_funds_portfolio : lit les fonds propres, le SCR et le MCR à analyser.
- check_tier_limits : calcule les fonds propres éligibles et contrôle les limites par Tier.
- generate_excel_qrt : écrit l'état QRT (S.23.01 ou S.25.01) dans un fichier Excel enregistré sur le serveur.

PROCÉDURE OBLIGATOIRE
1. Commence TOUJOURS par au moins une recherche (search_eiopa_vector_db) de la règle concernée, avant d'appliquer quoi que ce soit.
2. Lis les données avec get_own_funds_portfolio, puis calcule avec check_tier_limits.
3. N'appelle generate_excel_qrt que si l'utilisateur demande un état ou un fichier.

RÈGLES STRICTES
- Tous les chiffres de ta réponse viennent des outils : tu n'en inventes aucun, tu n'en recalcules aucun et tu ne fais aucune addition (utilise t2_plus_t3_classified, les valeurs de « controles », etc.).
- Cite les passages retrouvés par leur numéro [n] et leur source. Un passage ne confirme une règle que s'il l'ÉNONCE (les seuils eux-mêmes) : un passage sur le SCR ou le MCR en général ne la confirme pas, même avec une bonne pertinence. Déclare ton jugement dans check_tier_limits (rule_confirmed, evidence_ids) ; s'il est négatif, écris explicitement « règle non confirmée par la base documentaire » et précise que le calcul applique les limites de la formule standard intégrées à l'outil.
- Dans les résultats, PLAFONNÉ signifie « plafond d'éligibilité dépassé : l'excédent n'est pas éligible » (ce n'est pas une infraction) ; ÉCART signifie « minimum non atteint ».
- Ne prétends jamais avoir consulté les dernières guidelines de l'EIOPA : tu ne connais que ce que la base documentaire t'a renvoyé.
- check_tier_limits ne vérifie pas les critères qualitatifs de classement (subordination, durée, absence d'incitation au remboursement) : dis-le chaque fois que tu conclus sur un instrument.
- Quand tu génères un fichier, dis s'il est calculable par l'agent SCR (le résultat de l'outil l'indique) et, sinon, pourquoi : ne laisse jamais croire qu'un fichier sans modules de risque peut être calculé.
- Réponds en français, en phrases simples, sans mise en forme Markdown : le verdict d'abord, puis les chiffres clés, les sources, et le nom du fichier généré s'il y en a un."""


class AgentRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    own_funds: OwnFundsPortfolio
    solvency: dict[str, Any] | None = Field(
        None, description="S.25.01 lines that make the workbook computable by the SCR agent: modules [{name, value (net), brut?}] and details "
                          "{intangibles, operationalRisk, lacTp, lacDt, bscr?, diversificationBenefit?} (M EUR)")


class ToolCallTrace(BaseModel):
    tool: str
    args: dict[str, Any]
    result: str


class FileInfo(BaseModel):
    """What the client may know about a generated workbook. The server-side path is deliberately NOT part of it."""
    name: str
    template: str
    sheets: list[str]
    rows: int
    computable: bool = Field(False, description="The SCR agent can load and compute this workbook (it holds the S.25.01 risk modules)")
    warnings: list[str] = Field(default_factory=list)
    download_url: str


class AgentResponse(BaseModel):
    status: Literal["COMPLETED", "LLM_UNAVAILABLE"]
    answer: str | None
    rule_confirmed: bool | None = Field(None, description="The agent's verdict on whether the knowledge base states the applied rule; None if not assessed. Checked against the passages it cites")
    evidence_ids: list[int] = Field(default_factory=list)
    kb_max_relevance: float = Field(0.0, description="Best relevance of the passages retrieved (a retrieval score, NOT a confirmation)")
    sources: list[dict[str, Any]]
    eligibility: Eligibility | None
    file: FileInfo | None
    tool_calls: list[ToolCallTrace]
    model: str | None
    key_source: str | None = Field(None, description="Name of the environment variable of the key that answered (never the key itself)")
    errors: list[str] = Field(default_factory=list)


QUOTA_COOLDOWN_S = 600                       # a (key, model) pair that answered 429 is not retried for 10 minutes
_COOLDOWN: dict[tuple[str, str], float] = {}  # (key variable name, model) -> time until which it is skipped


def _api_keys() -> list[tuple[str, str]]:
    """Every configured Gemini key as (variable name, key), in priority order, without duplicates. Only the NAME is ever logged."""
    seen: set[str] = set()
    out = []
    for name in KEY_ENV_VARS:
        value = os.getenv(name)
        if value and value not in seen:
            seen.add(value)
            out.append((name, value))
    return out


def _is_quota_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RateLimit" in text


def _make_llm(model: str, key: str | None = None) -> ChatGoogleGenerativeAI:
    key = key or next((v for _n, v in _api_keys()), None)
    if not key:
        raise RuntimeError("Aucune clé Gemini (EIOPA_AGENT_API_KEY / GEMINI_API_KEY / INVESTIGATOR_API_KEY) dans rag_service/.env.")
    return ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.1, max_retries=3, timeout=60)


def _text(content: Any) -> str:
    """Gemini may answer with a plain string or a list of parts."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in content if isinstance(p, (str, dict))).strip()
    return ""


def _trace(messages: list[Any]) -> list[ToolCallTrace]:
    results = {m.tool_call_id: _text(m.content) for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if isinstance(m, AIMessage):
            for call in m.tool_calls:
                out.append(ToolCallTrace(tool=call["name"], args=call.get("args", {}), result=results.get(call["id"], "")[:600]))
    return out


def run_agent(request: AgentRequest, *, retriever: Retriever | None = None, llm_factory: Callable[[str], Any] | None = None) -> AgentResponse:
    ctx = AgentContext(question=request.question, portfolio=request.own_funds, solvency=request.solvency, retriever=retriever or default_retriever)
    # real run: every configured key x every model (keys outer: a quota is per key and per model); injected factory (tests): models only
    attempts: list[tuple[str, str | None, str | None]] = (
        [(model, name, key) for name, key in _api_keys() for model in MODELS] if llm_factory is None else [(model, None, None) for model in MODELS])
    if llm_factory is None and not attempts:
        attempts = [(MODELS[0], None, None)]                                  # no key at all: one attempt, which reports the missing key
    live = [a for a in attempts if _COOLDOWN.get((a[1] or "", a[0]), 0) <= time.time()]
    errors: list[str] = []
    for model, key_name, key in live or attempts:                            # everything cooling down: try anyway rather than fail blindly
        ctx.reset()
        label = f"{model} [{key_name}]" if key_name else model
        try:
            llm = llm_factory(model) if llm_factory else _make_llm(model, key)
            agent = create_agent(llm, build_tools(ctx), system_prompt=SYSTEM_PROMPT)
            result = agent.invoke({"messages": [HumanMessage(content=request.question)]}, config={"recursion_limit": RECURSION_LIMIT})
        except Exception as exc:               # noqa: BLE001 - quota / 5xx / tool error after retries: next key or model
            if _is_quota_error(exc):
                _COOLDOWN[(key_name or "", model)] = time.time() + QUOTA_COOLDOWN_S
            errors.append(f"{label} : {type(exc).__name__}: {str(exc)[:200]}")
            continue
        messages = result["messages"]
        answer = next((_text(m.content) for m in reversed(messages) if isinstance(m, AIMessage) and _text(m.content)), "")
        if not answer:
            errors.append(f"{label} : aucune réponse finale")
            continue
        return AgentResponse(
            status="COMPLETED", answer=answer, rule_confirmed=ctx.rule_confirmed, evidence_ids=ctx.evidence_ids, kb_max_relevance=ctx.kb_max_relevance,
            sources=ctx.sources, eligibility=ctx.eligibility,
            file=FileInfo(**ctx.file.model_dump(exclude={"path"}), download_url=f"/api/eiopa-agent/files/{ctx.file.name}") if ctx.file else None,
            tool_calls=_trace(messages), model=model, key_source=key_name, errors=errors)
    # the model never answered: the deterministic part is still worth returning
    ctx.reset()
    return AgentResponse(status="LLM_UNAVAILABLE", answer=None, sources=[], eligibility=ctx.ensure_eligibility(), file=None,
                         tool_calls=[], model=None, errors=errors)
