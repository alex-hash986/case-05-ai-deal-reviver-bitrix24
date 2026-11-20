from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.core.llm_gateway import rubles
from src.domain.schemas import DealSnapshot, StageSLA


@dataclass(frozen=True)
class Score:
    sla_excess_days: int
    stagnation_score: Decimal
    revival_probability: Decimal
    at_risk_revenue_rub: Decimal
    expected_recovered_rub: Decimal


def score_deal(deal: DealSnapshot, sla: StageSLA) -> Score:
    excess = max(0, deal.days_in_stage - sla.days)
    if not excess:
        return Score(0, Decimal(0), Decimal(0), Decimal(0), Decimal(0))
    index = min(Decimal(100), Decimal(4 * excess + 2 * deal.days_since_last_touch))
    probability = (sla.base_probability * (1 - index / 200)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP,
    )
    risk = deal.amount_rub * index / 100
    return Score(excess, index, probability, rubles(risk), rubles(risk * probability))
