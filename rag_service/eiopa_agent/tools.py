"""The three tools of the EIOPA & QRT agent, bound to ONE request (an AgentContext).

Design rule: the LLM chooses and sequences the tools, but never types a number. The portfolio lives in the context, the limits
are computed in Python, and the Excel file is written from the context. A wrong figure therefore cannot reach the file.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from langchain_core.tools import tool

from eiopa_agent.excel_qrt import GeneratedFile, write_qrt
from eiopa_agent.own_funds import TIER_LABEL, Eligibility, OwnFundsPortfolio, compute_eligibility

KB_CONFIRM_SCORE = 0.30     # reranker score from which a passage is considered to actually support a rule (min kept by the RAG is 0.05)
EXCERPT_CHARS = 700

Retriever = Callable[[str], list[dict[str, Any]]]


def default_retriever(query: str) -> list[dict[str, Any]]:
    """The notebook RAG: hybrid dense + BM25, reranked. Needs the service's models to be loaded (rag_core.init())."""
    import rag_core
    if rag_core.collection is None:
        raise RuntimeError("La base documentaire n'est pas initialisée (service RAG non démarré).")
    return rag_core.retrieve([query], mode="hybrid_rerank", top_k=4)


@dataclass
class AgentContext:
    question: str
    portfolio: OwnFundsPortfolio
    solvency: dict[str, Any] | None = None
    retriever: Retriever = default_retriever
    sources: list[dict[str, Any]] = field(default_factory=list)       # passages the agent actually retrieved, numbered
    eligibility: Eligibility | None = None
    file: GeneratedFile | None = None
    searches: int = 0
    rule_confirmed: bool | None = None                                 # the agent's declared verdict, checked against its evidence (None: not assessed)
    evidence_ids: list[int] = field(default_factory=list)

    @property
    def kb_max_relevance(self) -> float:
        return max((s["score"] for s in self.sources), default=0.0)

    def reset(self) -> None:
        self.sources, self.eligibility, self.file, self.searches = [], None, None, 0
        self.rule_confirmed, self.evidence_ids = None, []

    def ensure_eligibility(self) -> Eligibility:
        if self.eligibility is None:
            self.eligibility = compute_eligibility(self.portfolio)
        return self.eligibility


def build_tools(ctx: AgentContext) -> list:
    @tool
    def search_eiopa_vector_db(query: str) -> str:
        """Cherche dans la base documentaire réglementaire (Solvabilité II : directive, règlement délégué, cours, guidelines indexées).
        À utiliser AVANT d'appliquer une règle de calcul ou de classement. `query` : la règle cherchée, en français, avec ses mots-clés
        (ex. « limites Tier 2 Tier 3 fonds propres éligibles SCR »). Renvoie des passages numérotés [n] avec leur source, leur page et
        une pertinence entre 0 et 1."""
        ctx.searches += 1
        hits = ctx.retriever(query)
        if not hits:
            return ("Aucun passage pertinent trouvé dans la base documentaire pour cette requête. Règle NON confirmée par le corpus : "
                    "dis-le explicitement dans ta réponse.")
        lines = []
        for h in hits:
            meta = h["meta"]
            page = meta["page_start"] if meta["page_start"] == meta["page_end"] else f"{meta['page_start']}-{meta['page_end']}"
            excerpt = " ".join(h["text"].split())[:EXCERPT_CHARS]
            known = next((s for s in ctx.sources if s["source"] == meta["source"] and s["page"] == page and s["excerpt"][:80] == excerpt[:80]), None)
            if known is None:
                known = {"id": len(ctx.sources) + 1, "source": meta["source"], "page": page, "score": round(float(h["score"]), 3), "excerpt": excerpt}
                ctx.sources.append(known)
            lines.append(f"[{known['id']}] {known['source']} (p. {page}) — pertinence {known['score']:.2f}\n{excerpt}")
        return "\n\n".join(lines) + "\n\n(Une pertinence faible, sous 0,30, ne confirme pas une règle : signale-le.)"

    @tool
    def get_own_funds_portfolio() -> str:
        """Renvoie les fonds propres du portefeuille à analyser (instruments, classe de Tier, montants en M€) ainsi que le SCR et le MCR.
        Ce sont les seules données à utiliser : ne recopie ni ne modifie aucun montant."""
        p = ctx.portfolio
        return json.dumps({
            "date_de_reference": p.reference_date, "scr_meur": p.scr, "mcr_meur": p.mcr,
            "instruments": [{"nom": i.name, "classe": TIER_LABEL[i.tier], "nature": i.kind, "montant_meur": i.amount} for i in p.items],
        }, ensure_ascii=False)

    @tool
    def check_tier_limits(rule_confirmed: bool = False, evidence_ids: list[int] | None = None) -> str:
        """Calcule, en Python et à partir du portefeuille, les fonds propres éligibles et vérifie les limites quantitatives par Tier
        (Tier 1 >= 50 % du SCR, Tier 1 restreint <= 20 % du Tier 1, Tier 3 < 15 % du SCR, Tier 2 + Tier 3 <= 50 % du SCR, Tier 2 <= 20 % du MCR).
        Ne vérifie PAS les critères qualitatifs de classement (subordination, durée...). À appeler après avoir cherché la règle.
        `rule_confirmed` : true UNIQUEMENT si au moins un passage retrouvé ÉNONCE explicitement les limites appliquées (parler du SCR ou du
        MCR en général ne suffit pas) ; `evidence_ids` : les numéros [n] de ces passages. Sinon false et []. Le code vérifie tes preuves."""
        ids = sorted({i for i in (evidence_ids or [])})
        by_id = {s["id"]: s for s in ctx.sources}
        valid = [i for i in ids if i in by_id and by_id[i]["score"] >= KB_CONFIRM_SCORE]
        warning = None
        if rule_confirmed and not valid:
            warning = (f"Confirmation refusée : aucun des passages cités n'existe avec une pertinence >= {KB_CONFIRM_SCORE:.2f}. "
                       "La règle reste NON confirmée par la base documentaire.")
        ctx.rule_confirmed, ctx.evidence_ids = bool(rule_confirmed and valid), valid if rule_confirmed else []
        e = ctx.ensure_eligibility()
        out = e.model_dump(exclude={"checks"})
        out["controles"] = [{"controle": c.name, "valeur": c.value, "seuil": c.threshold, "statut": c.status, "commentaire": c.note} for c in e.checks]
        out["regle_confirmee_par_la_base"] = ctx.rule_confirmed
        out["preuves"] = ctx.evidence_ids
        out["avertissement"] = ["Critères qualitatifs de classement non vérifiés par cet outil.", "PLAFONNÉ = plafond d'éligibilité dépassé (excédent non éligible, pas une infraction) ; ÉCART = minimum non atteint."]
        if warning:
            out["avertissement"].append(warning)
        return json.dumps(out, ensure_ascii=False)

    @tool
    def generate_excel_qrt(template: Literal["S.23.01", "S.25.01"]) -> str:
        """Génère et enregistre sur le serveur un classeur Excel QRT prérempli à partir du portefeuille (jamais à partir de montants que tu
        fournirais). Le classeur contient les instruments, les fonds propres disponibles et, s'ils sont fournis, les modules de risque.
        Les résultats SCR/MCR et les ratios ne sont jamais préremplis : l'agent SCR les calcule ensuite. `template` : « S.23.01 »
        (état des fonds propres) ou « S.25.01 » (exige les modules de risque). Le résultat indique si le fichier est calculable par
        l'agent SCR : rapporte-le tel quel. À appeler seulement si l'utilisateur demande un état ou un fichier."""
        try:
            ctx.file = write_qrt(template, ctx.portfolio, ctx.ensure_eligibility(), ctx.sources, ctx.rule_confirmed, ctx.evidence_ids, ctx.question, ctx.solvency)
        except ValueError as exc:
            return f"Impossible de générer le fichier : {exc}"
        state = "non évaluée (check_tier_limits n'a pas été appelé)" if ctx.rule_confirmed is None else ("oui" if ctx.rule_confirmed else "non")
        computable = ("oui (feuilles S2501_SCR et Fonds_Propres_Ratios au format de l'import SolvaRisk)" if ctx.file.computable
                      else "NON : " + " ".join(ctx.file.warnings))
        extra = f" Remarques : {' '.join(ctx.file.warnings)}" if ctx.file.computable and ctx.file.warnings else ""
        return (f"Fichier généré et enregistré : {ctx.file.name} ({ctx.file.template}, {ctx.file.rows} lignes ; feuilles : {', '.join(ctx.file.sheets)}). "
                f"Calculable par l'agent SCR : {computable}.{extra} Règle confirmée par la base documentaire : {state}.")

    return [search_eiopa_vector_db, get_own_funds_portfolio, check_tier_limits, generate_excel_qrt]
