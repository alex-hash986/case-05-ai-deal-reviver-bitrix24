import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

from pydantic import TypeAdapter

from src.core.database import Database
from src.domain.schemas import Activity, DealPage, DemoFixture


class MockBitrix:
    def __init__(self, db: Database, fixture_path: Path, initialized_at: datetime) -> None:
        self.db = db
        fixtures = TypeAdapter(list[DemoFixture]).validate_json(fixture_path.read_bytes())
        self.fixtures = sorted(fixtures, key=lambda item: (item.updated_at, item.deal.deal_id))
        self.lock = asyncio.Lock()
        self.next_call = monotonic() + 0.5
        with db.connect() as connection:
            for item in self.fixtures:
                if item.cooldown_age_days is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO touch_history VALUES (?, ?, ?)",
                        (f"demo:{item.deal.deal_id}", item.deal.deal_id,
                         (initialized_at - timedelta(days=item.cooldown_age_days)).isoformat()),
                    )

    async def pace(self) -> None:
        async with self.lock:
            await asyncio.sleep(max(0.0, self.next_call - monotonic()))
            self.next_call = monotonic() + 0.5

    async def fetch_stale_deals(
        self, stages: list[str], limit: int, cursor: str | None, now: datetime,
    ) -> DealPage:
        await self.pace()
        position = None
        if cursor is not None:
            updated_at, deal_id = json.loads(cursor)
            position = datetime.fromisoformat(updated_at), int(deal_id)
        page = [
            item for item in self.fixtures
            if (not stages or item.deal.stage_id in stages)
            and (position is None or (item.updated_at, item.deal.deal_id) > position)
        ][:limit]
        deals = []
        for item in page:
            activities = list(item.deal.activities_history)
            if item.future_activity:
                activities.append(Activity(text="Запланированный звонок", completed=False,
                                           due_at=now + timedelta(days=2)))
            deals.append(item.deal.model_copy(update={
                "days_in_stage": item.stage_age_days,
                "days_since_last_touch": item.touch_age_days,
                "last_activity_at": now - timedelta(days=item.touch_age_days),
                "activities_history": activities,
            }))
        next_cursor = json.dumps([page[-1].updated_at.isoformat(), page[-1].deal.deal_id]) if page else cursor
        return DealPage(deals=deals, next_cursor=next_cursor)

    async def publish(self, operation_id: str, method: str, payload: Mapping[str, object]) -> str:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT receipt FROM mock_receipts WHERE operation_id = ?", (operation_id,),
            ).fetchone()
        if row:
            return str(row["receipt"])
        if method not in {"crm.timeline.comment.add", "tasks.task.add"} or "deal_id" not in payload:
            raise ValueError("Unsupported mock operation")
        await self.pace()
        receipt = f"mock:{operation_id}"
        with self.db.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO mock_receipts VALUES (?, ?)", (operation_id, receipt))
        return receipt
