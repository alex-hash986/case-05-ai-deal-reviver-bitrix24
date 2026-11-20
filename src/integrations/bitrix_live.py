import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from time import monotonic
from typing import Any

import httpx

from src.core.config import Settings
from src.core.database import Database
from src.domain.schemas import Activity, DealPage, DealSnapshot


class BitrixLiveAdapter:
    def __init__(self, settings: Settings, db: Database, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        url = httpx.URL(settings.bitrix_webhook_url.get_secret_value() if settings.bitrix_webhook_url else "")
        if url.scheme != "https" or not url.host or url.query or url.fragment:
            raise ValueError("BITRIX_WEBHOOK_URL must be an HTTPS webhook base URL")
        self.base_url = str(url).rstrip("/")
        self.request_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        self.next_call = 0.0

    async def _call(self, method: str, payload: Mapping[str, object]) -> dict[str, Any]:
        write = method.endswith(".add")
        for attempt in range(4):
            async with self.request_lock:
                await asyncio.sleep(max(0.0, self.next_call - monotonic()))
                try:
                    response = await self.client.post(f"{self.base_url}/{method}", json=dict(payload))
                except httpx.RequestError:
                    # Do not propagate a URL containing the webhook secret.
                    raise OSError(f"Bitrix transport failed for {method}") from None
                finally:
                    self.next_call = monotonic() + 0.5
            rate_limited = response.status_code == 429 or (
                response.status_code == 503 and self._query_limit_exceeded(response)
            )
            if attempt < 3 and (rate_limited or (response.status_code == 503 and not write)):
                retry_after = response.headers.get("Retry-After", "")
                await asyncio.sleep(max(0.5 * 2**attempt, int(retry_after) if retry_after.isdigit() else 0))
                continue
            if rate_limited:
                raise RateLimited(method)
            if response.status_code == 503:
                raise ServiceUnavailable(method)
            if not response.is_success:
                raise OSError(f"Bitrix HTTP {response.status_code} for {method}")
            data = response.json()
            if not isinstance(data, dict) or "result" not in data or data.get("error"):
                raise ValueError(f"Invalid Bitrix response for {method}")
            return data
        raise AssertionError("Unreachable retry state")

    @staticmethod
    def _query_limit_exceeded(response: httpx.Response) -> bool:
        try:
            data = response.json()
        except ValueError:
            return False
        return isinstance(data, dict) and data.get("error") == "QUERY_LIMIT_EXCEEDED"

    async def _rows(
        self, method: str, params: dict[str, object], *, result_key: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            page = await self._call(method, params)
            result = page["result"]
            yield_from = result[result_key] if result_key else result
            for row in yield_from:
                yield row
            if "next" not in page:
                return
            next_start = int(page["next"])
            if next_start <= int(str(params.get("start", 0))):
                raise ValueError("Bitrix pagination did not advance")
            params = {**params, "start": next_start}

    @staticmethod
    def _comment_params(deal_id: int) -> dict[str, object]:
        return {"filter": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"}, "order": {"ID": "ASC"}}

    async def current_assignee(self, deal_id: int) -> int:
        response = await self._call("crm.deal.get", {"id": deal_id})
        deal = response["result"]
        if deal.get("CLOSED") != "N":
            raise PermissionError("The deal is closed")
        return int(deal["ASSIGNED_BY_ID"])

    async def _snapshot(self, row: dict[str, Any], now: datetime) -> DealSnapshot:
        deal_id = int(row["ID"])
        comments = []
        last_touch = datetime.fromisoformat(row.get("LAST_ACTIVITY_TIME") or row["DATE_CREATE"])
        async for comment in self._rows("crm.timeline.comment.list", self._comment_params(deal_id)):
            if "[reviver:" not in comment["COMMENT"]:
                comments.append(comment["COMMENT"])
        activities = []
        async for entry in self._rows("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id}, "order": {"ID": "ASC"},
            "select": ["ID", "SUBJECT", "COMPLETED", "DEADLINE", "END_TIME"],
        }):
            due = entry.get("DEADLINE") or entry.get("END_TIME")
            activities.append(Activity(text=entry.get("SUBJECT") or "", completed=entry["COMPLETED"] == "Y",
                                       due_at=datetime.fromisoformat(due) if due else None))
        moved = datetime.fromisoformat(row.get("MOVED_TIME") or row["DATE_CREATE"])
        field = self.settings.bitrix_verified_terms_field
        terms = row.get(field) if field else None
        if isinstance(terms, str):
            terms = [terms]
        if row.get("CURRENCY_ID") != "RUB":
            raise ValueError("Live pipeline must use RUB")
        return DealSnapshot(
            deal_id=deal_id, title=row["TITLE"], stage_id=row["STAGE_ID"], stage_name=row["STAGE_ID"],
            amount_rub=row.get("OPPORTUNITY") or "0", assigned_by_id=int(row["ASSIGNED_BY_ID"]),
            assigned_by_name=f"ID {row['ASSIGNED_BY_ID']}", last_activity_at=last_touch,
            days_in_stage=max(0, (now - moved).days), days_since_last_touch=max(0, (now - last_touch).days),
            comments_history=comments, activities_history=activities, verified_commercial_terms=terms or [],
        )

    async def fetch_stale_deals(
        self, stages: list[str], limit: int, cursor: str | None, now: datetime,
    ) -> DealPage:
        select = ["ID", "TITLE", "STAGE_ID", "DATE_MODIFY", "DATE_CREATE", "MOVED_TIME",
                  "LAST_ACTIVITY_TIME", "ASSIGNED_BY_ID", "OPPORTUNITY", "CURRENCY_ID"]
        if self.settings.bitrix_verified_terms_field:
            select.append(self.settings.bitrix_verified_terms_field)
        filters: dict[str, object] = {"CLOSED": "N", "CURRENCY_ID": "RUB"}
        if stages:
            filters["@STAGE_ID"] = stages
        if cursor:
            date, deal_id = json.loads(cursor)
            windows = [{"=DATE_MODIFY": date, ">ID": deal_id}, {">DATE_MODIFY": date}]
        elif self.settings.bitrix_initial_modified_since:
            windows = [{">=DATE_MODIFY": self.settings.bitrix_initial_modified_since}]
        else:
            windows = [{}]
        deals = []
        for window in windows:
            async for row in self._rows("crm.deal.list", {
                "filter": {**filters, **window}, "select": select,
                "order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
            }):
                deals.append(await self._snapshot(row, now))
                cursor = json.dumps([row["DATE_MODIFY"], int(row["ID"])])
                if len(deals) == limit:
                    return DealPage(deals=deals, next_cursor=cursor)
        return DealPage(deals=deals, next_cursor=cursor)

    async def publish(self, operation_id: str, method: str, payload: Mapping[str, object]) -> str:
        async with self.publish_lock:
            return await self._publish(operation_id, method, payload)

    async def _publish(self, operation_id: str, method: str, payload: Mapping[str, object]) -> str:
        deal_id = int(str(payload["deal_id"]))
        marker = f"[reviver:{operation_id}]"
        if method == "crm.timeline.comment.add":
            receipt_key = "comment_id"
            fields = {
                "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal",
                "COMMENT": f"{payload['comment']}\nРиск: {payload['at_risk_revenue_rub']} ₽. "
                           f"Прогноз: {payload['expected_recovered_rub']} ₽.\n{marker}",
            }
            lookup_method, lookup_filter, lookup_key = (
                "crm.timeline.comment.list", self._comment_params(deal_id), None,
            )
            id_field, text_field = "ID", "COMMENT"
        elif method == "tasks.task.add":
            receipt_key = "task_id"
            binding = [f"D_{deal_id}"]
            fields = {
                "TITLE": payload["title"], "RESPONSIBLE_ID": payload["responsible_id"],
                "DESCRIPTION": f"{payload['description']}\n\n{marker}", "UF_CRM_TASK": binding,
            }
            lookup_method, lookup_filter, lookup_key = "tasks.task.list", {
                "filter": {"UF_CRM_TASK": binding}, "select": ["ID", "DESCRIPTION"],
            }, "tasks"
            id_field, text_field = "id", "description"
        else:
            raise ValueError("Unsupported Bitrix operation")

        with self.db.connect() as connection:
            first_attempt = connection.execute(
                "INSERT OR IGNORE INTO live_writes VALUES (?)", (operation_id,),
            ).rowcount == 1
        lookup_attempts = 1
        if first_attempt:
            try:
                response = await self._call(method, {"fields": fields})
            except RateLimited:
                with self.db.connect() as connection:
                    connection.execute("DELETE FROM live_writes WHERE operation_id=?", (operation_id,))
                raise
            except ServiceUnavailable:
                lookup_attempts = 3
            else:
                result = response["result"]
                remote_id = result["task"]["id"] if method == "tasks.task.add" else result
                return json.dumps({receipt_key: int(remote_id)})
        for attempt in range(lookup_attempts):
            if lookup_attempts > 1:
                await asyncio.sleep(0.5 * 2**attempt)
            async for row in self._rows(lookup_method, lookup_filter, result_key=lookup_key):
                if marker in str(row.get(text_field, "")):
                    return json.dumps({receipt_key: int(row[id_field])})
        raise OSError("Bitrix write outcome is still unknown")


class RateLimited(OSError):
    pass


class ServiceUnavailable(OSError):
    pass
