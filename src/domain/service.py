import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hmac import compare_digest
from time import perf_counter
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import TypeAdapter

from src.core.config import ROOT, Settings
from src.core.database import Database
from src.core.llm_gateway import BudgetExceeded, CycleUsage, LLMGateway, known_fields, rubles
from src.domain.guardrails import CommercialPolicyGuard, customer_declined, eligibility_reason
from src.domain.ports import BitrixPort
from src.domain.schemas import (
    DealPage,
    DealSnapshot,
    Draft,
    Metrics,
    RevivalDecisionOut,
    ScanIn,
    ScanOut,
    StageSLA,
)
from src.domain.scoring import score_deal


class Conflict(Exception):
    pass


class Reviver:
    def __init__(
        self, settings: Settings, db: Database, bitrix: BitrixPort, gateway: LLMGateway,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        current_assignee: Callable[[int], Awaitable[int]] | None = None,
    ) -> None:
        import yaml

        self.settings = settings
        self.db = db
        self.bitrix = bitrix
        self.gateway = gateway
        self.clock = clock
        self.current_assignee = current_assignee
        self.lock = asyncio.Lock()
        self.slas = TypeAdapter(dict[str, StageSLA]).validate_python(
            yaml.safe_load((ROOT / "config/stage_sla.yaml").read_text(encoding="utf-8")),
        )
        self.policy = CommercialPolicyGuard.model_validate(
            yaml.safe_load((ROOT / "config/commercial_policy.yaml").read_text(encoding="utf-8")),
        )
        self.prompt = gateway.load_prompt(ROOT / "config/revival_prompt.yaml", settings.reviver_variant)
        self.mock_drafts = TypeAdapter(dict[str, Draft]).validate_python(
            yaml.safe_load((ROOT / ("config/demo_drafts_b.yaml" if settings.reviver_variant == "variant_b"
                       else "config/demo_drafts.yaml")).read_text(encoding="utf-8")),
        )

    async def scan(self, key: str, request: ScanIn) -> ScanOut:
        async with self.lock:
            return await self._scan(key, request)

    async def _scan(self, key: str, request: ScanIn) -> ScanOut:
        start = perf_counter()
        now = self.clock()
        hour = now.astimezone(ZoneInfo("Asia/Novosibirsk")).hour
        payload_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        scope = json.dumps(request.stages)
        with self.db.connect() as connection:
            run = connection.execute("SELECT * FROM scan_runs WHERE request_id=?", (key,)).fetchone()
            if run and run["payload_hash"] != payload_hash:
                raise Conflict("Idempotency-Key already used for a different payload")
            if run and run["response"]:
                return ScanOut.model_validate_json(run["response"])
            if run and (run["mode"], run["ab_variant"]) != (
                self.settings.llm_mode, self.settings.reviver_variant
            ):
                raise Conflict("Incomplete scan was started in another LLM mode")
            connection.execute(
                """INSERT OR IGNORE INTO scan_runs
                   (request_id,payload_hash,session_id,mode,is_after_hours,ab_variant) VALUES (?,?,?,?,?,?)""",
                (key, payload_hash, request.session_id, self.settings.llm_mode, not 9 <= hour < 18,
                 self.settings.reviver_variant),
            )
            cursor = connection.execute("SELECT cursor FROM scan_cursors WHERE scope=?", (scope,)).fetchone()
            others = connection.execute(
                "SELECT cost_rub,cost_complete FROM scan_runs WHERE session_id=? AND request_id<>?",
                (request.session_id, key),
            ).fetchall()
        usage = CycleUsage(
            cost_rub=Decimal(run["cost_rub"]) if run else Decimal(0),
            tokens_in=run["tokens_in"] if run else 0,
            tokens_out=run["tokens_out"] if run else 0,
            cost_complete=bool(run["cost_complete"]) if run else True,
            spending_limit=(self.settings.max_session_cost_rub - sum(
                Decimal(row["cost_rub"]) for row in others
            ) if all(row["cost_complete"] for row in others) else Decimal(-1)),
            on_change=lambda current: self._record_usage(key, current),
        )
        page = await self._fetch(key, request, cursor["cursor"] if cursor else None, now)
        decisions = [await self._decision(deal, now, usage) for deal in page.deals]
        with self.db.connect() as connection:
            for deal, item in zip(page.deals, decisions, strict=True):
                if item.should_revive:
                    self._enqueue(connection, deal, item, now)
            saved_input = connection.execute(
                "SELECT payload FROM bitrix_steps WHERE operation_id=?", (f"scan:{key}",),
            ).fetchone()
            if page.next_cursor is not None and json.loads(saved_input["payload"])["cursor"] == (
                cursor["cursor"] if cursor else None
            ):
                connection.execute("INSERT OR REPLACE INTO scan_cursors VALUES (?,?)", (scope, page.next_cursor))
            response = ScanOut(request_id=key, next_cursor=page.next_cursor, decisions=decisions)
            connection.execute(
                "UPDATE scan_runs SET response=?,processing_time_sec=?,need_human=? WHERE request_id=?",
                (response.model_dump_json(), perf_counter() - start, any(x.need_human for x in decisions), key),
            )
        await self._reconcile()
        return response

    def _record_usage(self, key: str, usage: CycleUsage) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE scan_runs SET cost_rub=?,tokens_in=?,tokens_out=?,cost_complete=? WHERE request_id=?",
                (str(usage.cost_rub), usage.tokens_in, usage.tokens_out, usage.cost_complete, key),
            )

    async def _fetch(self, key: str, request: ScanIn, cursor: str | None, now: datetime) -> DealPage:
        operation = f"scan:{key}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO bitrix_steps (operation_id,method,payload) VALUES (?,?,?)",
                (operation, "fetch_stale_deals", json.dumps({"request": request.model_dump(), "cursor": cursor})),
            )
            row = connection.execute(
                "SELECT receipt,payload FROM bitrix_steps WHERE operation_id=?", (operation,),
            ).fetchone()
            if row["receipt"]:
                return DealPage.model_validate_json(row["receipt"])
            saved_cursor = json.loads(row["payload"])["cursor"]
            connection.execute(
                "UPDATE bitrix_steps SET state='sending',attempts=attempts+1 WHERE operation_id=?", (operation,),
            )
        try:
            page = await self.bitrix.fetch_stale_deals(request.stages, request.limit, saved_cursor, now)
        except Exception as error:
            self._finish_step(operation, error=type(error).__name__)
            raise
        self._finish_step(operation, receipt=page.model_dump_json())
        return page

    async def _decision(self, deal: DealSnapshot, now: datetime, usage: CycleUsage) -> RevivalDecisionOut:
        start = perf_counter()
        item = RevivalDecisionOut(
            revival_id=uuid4().hex, deal_id=deal.deal_id, deal_title=deal.title,
            assigned_by_name=deal.assigned_by_name,
        )
        sla = self.slas.get(deal.stage_id)
        with self.db.connect() as connection:
            last = connection.execute(
                "SELECT MAX(touched_at) FROM touch_history WHERE deal_id=?", (deal.deal_id,),
            ).fetchone()[0]
            pending = connection.execute(
                """SELECT 1 FROM revival_items WHERE deal_id=?
                   AND json_extract(result, '$.approval_status')='pending_approval'""",
                (deal.deal_id,),
            ).fetchone()
        reason = eligibility_reason(
            deal, sla, now, datetime.fromisoformat(last) if last else None, self.settings.revival_cooldown_days,
        )
        if pending and reason is None:
            reason = "already_pending"
        if reason is not None:
            item.skip_reason = reason
        elif sla is not None:
            values = asdict(score_deal(deal, sla))
            values.pop("sla_excess_days")
            item = RevivalDecisionOut.model_validate({**item.model_dump(), **values})
            before = usage.cost_rub
            await self._draft(item, deal, usage)
            item.cost_rub = rubles(usage.cost_rub - before)
        item.latency_ms = round((perf_counter() - start) * 1000)
        return item

    async def _draft(self, item: RevivalDecisionOut, deal: DealSnapshot, usage: CycleUsage) -> None:
        history = " ".join([*deal.comments_history, *(a.text for a in deal.activities_history)]).casefold()
        if customer_declined(history):
            item.stall_reason_category = "negative"
            item.stall_summary = self.mock_drafts["negative"].stall_summary
            item.skip_reason = "customer_declined"
            return
        if not any(comment.strip() for comment in deal.comments_history) and not deal.activities_history:
            item.skip_reason = "insufficient_history"
            item.need_human = True
            return
        try:
            if self.settings.llm_mode == "mock":
                category = next(
                    (category for marker, category in (
                        ("согласован", "internal_review"),
                        ("бюджет", "budget"), ("без ответа", "no_response"),
                    ) if marker in history), "unknown",
                )
                draft = self.mock_drafts[category]
                if self.settings.reviver_variant == "variant_b":
                    evidence = [*deal.comments_history, *(a.text for a in deal.activities_history if a.completed)]
                    for subject in reversed(evidence):
                        artifact = subject.strip().rstrip(".!?")
                        if artifact and len(artifact) <= 160 and self.policy.allows(artifact, deal):
                            draft = draft.model_copy(update={
                                "draft_message": draft.draft_message.replace("{artifact}", artifact),
                            })
                            break
                    else:
                        draft = draft.model_copy(update={
                            "draft_message": draft.draft_message.replace("{artifact}", "предыдущего обсуждения"),
                        })
            else:
                raw = await self.gateway.evaluate(self.prompt, deal.model_dump_json(), usage, model="smart")
                draft = Draft.model_validate(known_fields(raw, Draft))
        except BudgetExceeded:
            item.skip_reason = "budget_exhausted"
            item.need_human = True
            return
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            item.skip_reason = "llm_unavailable"
            item.need_human = True
            return
        item.stall_reason_category = draft.stall_reason_category
        item.stall_summary = draft.stall_summary
        if draft.stall_reason_category == "negative":
            item.skip_reason = "customer_declined"
            return
        if not all(self.policy.allows(text, deal)
                   for text in (draft.draft_message, draft.next_best_action)):
            item.skip_reason = "commercial_policy"
            item.need_human = True
            return
        item.draft_message = draft.draft_message
        item.next_best_action = draft.next_best_action
        item.should_revive = item.need_human = True
        item.approval_status = "pending_approval"
        item.bitrix_sync_status = "pending"

    def _enqueue(
        self, connection: sqlite3.Connection, deal: DealSnapshot, item: RevivalDecisionOut, now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO revival_items VALUES (?,?,?,?,?)",
            (item.revival_id, item.deal_id, item.model_dump_json(), deal.title, deal.assigned_by_id),
        )
        connection.execute("INSERT INTO touch_history VALUES (?,?,?)",
                           (item.revival_id, item.deal_id, now.isoformat()))
        self._plan(connection, item, "timeline", "crm.timeline.comment.add", {
            "comment": item.stall_summary, "at_risk_revenue_rub": str(item.at_risk_revenue_rub),
            "expected_recovered_rub": str(item.expected_recovered_rub),
        })
        if self.settings.bitrix_mode == "mock":
            self._plan(connection, item, "review", "tasks.task.add", {
                "title": "Проверить черновик реанимации", "responsible_id": deal.assigned_by_id,
            })

    @staticmethod
    def _plan(
        connection: sqlite3.Connection, item: RevivalDecisionOut, name: str, method: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO bitrix_steps (operation_id,revival_id,method,payload) VALUES (?,?,?,?)",
            (f"{item.revival_id}:{name}", item.revival_id, method,
             json.dumps({"deal_id": item.deal_id, **payload}, ensure_ascii=False)),
        )

    async def approve(
        self, revival_id: str, approve: bool, approver_user_id: int | None = None, token: str = "",
    ) -> RevivalDecisionOut | None:
        async with self.lock:
            with self.db.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM revival_items WHERE revival_id=?", (revival_id,),
                ).fetchone()
                if row is None:
                    return None
                item = RevivalDecisionOut.model_validate_json(row["result"])
                if self.settings.bitrix_mode == "live" or self.settings.bitrix_approver_tokens:
                    credential = self.settings.bitrix_approver_tokens.get(approver_user_id or 0)
                    if (
                        credential is None or not credential.get_secret_value()
                        or not compare_digest(credential.get_secret_value().encode(), token.encode())
                    ):
                        raise PermissionError("Manager cannot decide this revival")
                responsible = (
                    await self.current_assignee(item.deal_id) if self.current_assignee else row["assigned_by_id"]
                )
                if (self.settings.bitrix_mode == "live" or self.settings.bitrix_approver_tokens) and (
                    approver_user_id != responsible and approver_user_id not in self.settings.bitrix_supervisor_ids
                ):
                    raise PermissionError("Manager cannot decide this revival")
                status: Literal["approved", "rejected"] = "approved" if approve else "rejected"
                if item.approval_status == "pending_approval":
                    item.approval_status = status
                    item.approver_user_id = approver_user_id
                    if approve:
                        item.bitrix_sync_status = "pending"
                        connection.execute(
                            "UPDATE touch_history SET touched_at=? WHERE revival_id=?",
                            (self.clock().isoformat(), revival_id),
                        )
                        self._plan(connection, item, "approved_touch", "tasks.task.add", {
                            "title": "Выполнить одобренное касание", "description": item.draft_message,
                            "responsible_id": responsible,
                        })
                    self._save(connection, item)
                elif item.approval_status != status:
                    raise Conflict("Approval decision already recorded")
            await self._reconcile()
            with self.db.connect() as connection:
                result = connection.execute(
                    "SELECT result FROM revival_items WHERE revival_id=?", (revival_id,),
                ).fetchone()["result"]
            return RevivalDecisionOut.model_validate_json(result)

    @staticmethod
    def _save(connection: sqlite3.Connection, item: RevivalDecisionOut) -> None:
        connection.execute("UPDATE revival_items SET result=? WHERE revival_id=?",
                           (item.model_dump_json(), item.revival_id))

    async def reconcile(self) -> int:
        async with self.lock:
            return await self._reconcile()

    async def _reconcile(self) -> int:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM bitrix_steps
                   WHERE revival_id IS NOT NULL AND state<>'succeeded' ORDER BY rowid""",
            ).fetchall()
        blocked = set()
        completed = 0
        for row in rows:
            revival = row["revival_id"]
            if revival in blocked:
                continue
            operation = row["operation_id"]
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE bitrix_steps SET state='sending',attempts=attempts+1 WHERE operation_id=?", (operation,),
                )
            try:
                receipt = await self.bitrix.publish(operation, row["method"], json.loads(row["payload"]))
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
                self._finish_step(operation, error=type(error).__name__)
                blocked.add(revival)
            else:
                self._finish_step(operation, receipt=receipt)
                completed += 1
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT result FROM revival_items i
                   WHERE json_extract(i.result, '$.bitrix_sync_status')='pending'
                   AND NOT EXISTS (SELECT 1 FROM bitrix_steps s
                                   WHERE s.revival_id=i.revival_id AND s.state<>'succeeded')""",
            ).fetchall()
            for row in rows:
                item = RevivalDecisionOut.model_validate_json(row["result"])
                item.bitrix_sync_status = "synced"
                self._save(connection, item)
        return completed

    def _finish_step(self, operation: str, *, receipt: str | None = None, error: str | None = None) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE bitrix_steps SET state=?,receipt=?,last_error=? WHERE operation_id=?",
                ("succeeded" if receipt is not None else "pending", receipt, error, operation),
            )

    def queue(self) -> list[RevivalDecisionOut]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT result FROM revival_items ORDER BY rowid DESC").fetchall()
        return [RevivalDecisionOut.model_validate_json(row["result"]) for row in rows]

    def metrics(self) -> Metrics:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM scan_runs").fetchall()
        responses = [ScanOut.model_validate_json(row["response"]) for row in rows if row["response"]]
        latest_by_deal: dict[int, RevivalDecisionOut] = {}
        queue = self.queue()
        for item in reversed(queue):
            latest_by_deal[item.deal_id] = item
        active = [item for item in latest_by_deal.values() if item.approval_status != "rejected"]
        decisions = [item for response in responses for item in response.decisions]
        return Metrics(
            at_risk_revenue_rub=sum((item.at_risk_revenue_rub for item in active), Decimal(0)),
            expected_recovered_rub=sum((item.expected_recovered_rub for item in active), Decimal(0)),
            approval_rate=(Decimal(sum(item.approval_status == "approved" for item in queue)) / len(queue)
                           if queue else Decimal(0)),
            average_latency_ms=(rubles(Decimal(sum(item.latency_ms for item in decisions)) / len(decisions))
                                if decisions else Decimal(0)),
            cost_rub=rubles(sum((Decimal(row["cost_rub"]) for row in rows), Decimal(0))),
            cost_complete=all(row["cost_complete"] for row in rows),
            queue_size=sum(item.approval_status == "pending_approval" for item in queue),
            scans=len(responses),
        )
