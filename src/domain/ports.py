from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from src.domain.schemas import DealPage


class BitrixPort(Protocol):
    async def fetch_stale_deals(
        self, stages: list[str], limit: int, cursor: str | None, now: datetime,
    ) -> DealPage: ...

    async def publish(self, operation_id: str, method: str, payload: Mapping[str, object]) -> str: ...
