"""Own funds: classification limits per Tier and eligible amounts (deterministic, no LLM).

Same rules as the SCR engine of the n8n workflow (so both give the same eligible own funds):
  SCR  - Tier 1 >= 50 % of the SCR ; restricted Tier 1 <= 20 % of Tier 1 (the excess is reclassified in Tier 2) ;
         Tier 3 < 15 % of the SCR ; Tier 2 + Tier 3 <= 50 % of the SCR.
  MCR  - Tier 3 excluded ; Tier 2 <= 20 % of the MCR.
Only the QUANTITATIVE limits are checked. The qualitative criteria that decide whether an instrument is Tier 2 at all
(subordination, duration, no incentive to redeem...) are not, and the result says so.
"""
import math
from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["T1_UNRESTRICTED", "T1_RESTRICTED", "T2", "T3"]
TIER_LABEL = {"T1_UNRESTRICTED": "Tier 1 non restreint", "T1_RESTRICTED": "Tier 1 restreint", "T2": "Tier 2", "T3": "Tier 3"}


class OwnFundsItem(BaseModel):
    name: str = Field(description="Instrument or own funds item, e.g. 'Obligations subordonnées 2034'")
    tier: Tier
    amount: float = Field(ge=0, description="Amount in M EUR")
    kind: str | None = Field(None, description="Nature: capital, réserves, dette subordonnée, DTA...")


class OwnFundsPortfolio(BaseModel):
    items: list[OwnFundsItem] = Field(min_length=1)
    scr: float = Field(gt=0, description="SCR (M EUR)")
    mcr: float = Field(ge=0, description="MCR (M EUR)")
    mcr_linear: float | None = Field(None, ge=0, description="Linear MCR, S.28.01-R0010 (optional, makes the MCR computable by the SCR engine)")
    amcr: float | None = Field(None, ge=0, description="Absolute floor of the MCR (AMCR), S.28.01-R0330 (optional)")
    reference_date: str | None = None


class LimitCheck(BaseModel):
    name: str
    rule: str
    value: float
    threshold: float
    ok: bool
    note: str = ""
    kind: Literal["floor", "cap"] = Field("floor", description="floor: a minimum to reach (shortfall = ÉCART); cap: an eligibility ceiling (excess is simply not eligible = PLAFONNÉ)")

    @property
    def status(self) -> str:
        return "OK" if self.ok else ("PLAFONNÉ" if self.kind == "cap" else "ÉCART")


class Eligibility(BaseModel):
    t1_unrestricted: float
    t1_restricted: float
    t2: float
    t3: float
    t2_plus_t3_classified: float = Field(description="Tier 2 (incl. reclassified Tier 1) + Tier 3 as classified, before the limits")
    available: float
    t1_restricted_eligible: float
    reclassified_to_t2: float
    t1_eligible: float
    t2_eligible: float
    t3_eligible: float
    eligible_scr: float
    eligible_mcr: float
    haircut: float = Field(description="Available own funds not eligible because of the limits")
    ratio_scr_pct: float
    ratio_mcr_pct: float | None
    checks: list[LimitCheck]
    qualitative_criteria_checked: bool = False


def _sum(portfolio: OwnFundsPortfolio, tier: str) -> float:
    return sum(i.amount for i in portfolio.items if i.tier == tier)


def round_half_up(x: float) -> float:
    """Same rounding as the n8n engine (JavaScript Math.round): 372.085 -> 372.09. Python's round() would give 372.08."""
    return math.floor(x * 100 + 0.5) / 100


def compute_eligibility(portfolio: OwnFundsPortfolio) -> Eligibility:
    scr, mcr = portfolio.scr, portfolio.mcr
    u, r, t2, t3 = (_sum(portfolio, t) for t in ("T1_UNRESTRICTED", "T1_RESTRICTED", "T2", "T3"))

    r_elig = min(r, 0.25 * u)                      # restricted <= 20 % of total Tier 1  <=>  restricted <= 25 % of unrestricted
    reclassified = r - r_elig
    t1_elig = u + r_elig
    t2_avail = t2 + reclassified
    t3_elig = min(t3, 0.15 * scr)
    t2_elig = min(t2_avail, max(0.0, 0.5 * scr - t3_elig))
    eligible_scr = t1_elig + t2_elig + t3_elig
    eligible_mcr = t1_elig + min(t2_avail, 0.2 * mcr)      # MCR: no Tier 3, Tier 2 capped at 20 % of the MCR
    available = u + r + t2 + t3

    checks = [
        LimitCheck(name="Tier 1 ≥ 50 % du SCR", rule="Tier 1 éligible >= 0,5 x SCR", value=t1_elig, threshold=0.5 * scr, ok=t1_elig >= 0.5 * scr),
        LimitCheck(name="Tier 1 restreint ≤ 20 % du Tier 1", rule="Tier 1 restreint <= 0,2 x (Tier 1 total)", value=r, threshold=0.2 * (u + r), ok=r <= 0.2 * (u + r) + 1e-9, kind="cap",
                   note=f"{reclassified:.2f} M€ reclassés en Tier 2" if reclassified > 1e-9 else ""),
        LimitCheck(name="Tier 3 < 15 % du SCR", rule="Tier 3 < 0,15 x SCR", value=t3, threshold=0.15 * scr, ok=t3 < 0.15 * scr, kind="cap",
                   note=f"{t3 - t3_elig:.2f} M€ de Tier 3 non éligibles" if t3 > 0.15 * scr else ""),
        LimitCheck(name="Tier 2 + Tier 3 ≤ 50 % du SCR", rule="Tier 2 + Tier 3 éligibles <= 0,5 x SCR", value=t2_avail + t3_elig, threshold=0.5 * scr, ok=t2_avail + t3_elig <= 0.5 * scr + 1e-9, kind="cap",
                   note=f"{t2_avail - t2_elig:.2f} M€ de Tier 2 non éligibles" if t2_avail > t2_elig + 1e-9 else ""),
    ]
    if mcr > 0:
        checks.append(LimitCheck(name="Tier 2 ≤ 20 % du MCR", rule="Tier 2 (couverture du MCR) <= 0,2 x MCR", value=t2_avail, threshold=0.2 * mcr, ok=t2_avail <= 0.2 * mcr + 1e-9, kind="cap",
                                 note=(f"{t2_avail - 0.2 * mcr:.2f} M€ de Tier 2 non éligibles pour le MCR (Tier 3 exclu du MCR)" if t2_avail > 0.2 * mcr + 1e-9 else "")))
    checks.append(LimitCheck(name="Ratio de couverture SCR ≥ 100 %", rule="Fonds propres éligibles / SCR >= 100 %", value=eligible_scr / scr * 100, threshold=100.0, ok=eligible_scr >= scr))
    if mcr > 0:
        checks.append(LimitCheck(name="Ratio de couverture MCR ≥ 100 %", rule="Fonds propres éligibles MCR / MCR >= 100 %", value=eligible_mcr / mcr * 100, threshold=100.0, ok=eligible_mcr >= mcr))

    r2 = round_half_up
    for c in checks:
        c.value, c.threshold = r2(c.value), r2(c.threshold)
    return Eligibility(
        t1_unrestricted=r2(u), t1_restricted=r2(r), t2=r2(t2), t3=r2(t3), t2_plus_t3_classified=r2(t2_avail + t3), available=r2(available),
        t1_restricted_eligible=r2(r_elig), reclassified_to_t2=r2(reclassified), t1_eligible=r2(t1_elig), t2_eligible=r2(t2_elig), t3_eligible=r2(t3_elig),
        eligible_scr=r2(eligible_scr), eligible_mcr=r2(eligible_mcr), haircut=r2(available - eligible_scr),
        ratio_scr_pct=r2(eligible_scr / scr * 100), ratio_mcr_pct=r2(eligible_mcr / mcr * 100) if mcr > 0 else None, checks=checks)
