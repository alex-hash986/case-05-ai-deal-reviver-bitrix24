import re
import unicodedata
from datetime import datetime, timedelta

from src.domain.schemas import DTO, DealSnapshot, StageSLA


def eligibility_reason(
    deal: DealSnapshot, sla: StageSLA | None, now: datetime,
    last_revival: datetime | None, cooldown_days: int,
) -> str | None:
    if sla is None:
        return "unconfigured_stage"
    if deal.days_in_stage <= sla.days:
        return "within_sla"
    if any(not item.completed and item.due_at is not None and item.due_at > now
           for item in deal.activities_history):
        return "future_activity"
    if last_revival and now - last_revival < timedelta(days=cooldown_days):
        return "cooldown"
    return None


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def customer_declined(history: str) -> bool:
    return re.search(
        r"не\s+(?:связываться|беспокоить|звонить|писать)|отписал(?:ся|ась|ись)|отозвал[аи]? согласие|"
        r"закрыли потребность|выбрали конкурента|купили у конкурента|конфликтная эскалация|"
        r"эскалирован[ао]? в юридическ",
        normalize(history),
    ) is not None


class CommercialPolicyGuard(DTO):
    restricted_pattern: str
    approved_terms: list[str]

    def allows(self, draft: str, deal: DealSnapshot) -> bool:
        remaining = normalize(draft)
        verified = {normalize(term) for term in deal.verified_commercial_terms}
        approved = {normalize(term) for term in self.approved_terms} & verified
        if remaining.rstrip(".!?").rstrip() in approved - {""}:
            return True
        return re.search(self.restricted_pattern, remaining) is None
