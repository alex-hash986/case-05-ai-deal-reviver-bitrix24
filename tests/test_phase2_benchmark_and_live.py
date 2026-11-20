import asyncio
import json
from collections import Counter
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ops.runners.benchmark_ab import load_cases, run
from src.api.main import create_app
from src.core.config import Settings
from src.core.database import Database

PREFIX = "/api/v1/reviver"
DATE = "2026-01-01T12:00:00+00:00"


def test_dataset_balance() -> None:
    cases = load_cases()
    assert len({case.case_id for case in cases}) == len({case.deal.deal_id for case in cases}) == 60
    assert Counter(case.category for case in cases) == {
        "revive": 26, "eligibility_exclusion": 14, "negative_exclusion": 10, "commercial_policy_trap": 10,
    }
    assert sum(case.expected_should_revive for case in cases) == 36
    assert {case.expected_skip_reason for case in cases} == {
        None, "within_sla", "future_activity", "cooldown", "already_pending", "customer_declined",
    }
    assert {case.historical_won for case in cases} == {True, False}


def test_benchmark_mock(tmp_path: Path) -> None:
    asyncio.run(run("mock", tmp_path))
    report = json.loads((tmp_path / "benchmark_ab_report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "mock"
    assert report["synthetic_outcomes"]
    assert set(report["variants"]) == {"variant_a", "variant_b"}
    for result in report["variants"].values():
        assert result["cases"] == 60
        assert result["eligibility_accuracy"] == result["opt_out_cooldown_recall"] == 1
        assert result["commercial_policy_compliance_rate"] == 1
        assert result["unauthorized_offers"] == 0
        assert result["policy_probes_blocked"] == 10
        assert result["verified_terms_allowed"] == 2
    assert (tmp_path / "benchmark_ab_report.md").is_file()


@pytest.mark.parametrize("ambiguous_status", [None, 503])
def test_live_approval_rate_limit_and_reconcile(tmp_path: Path, ambiguous_status: int | None) -> None:
    calls: list[tuple[str, float, dict[str, Any]]] = []
    tasks: list[dict[str, Any]] = []
    comments = [{"ID": "1", "COMMENT": "КП отправлено, без ответа"}]
    owner = 7
    deal = {
        "ID": "94", "TITLE": "Тестовая сделка", "STAGE_ID": "PROPOSAL", "DATE_CREATE": DATE,
        "DATE_MODIFY": DATE, "ASSIGNED_BY_ID": str(owner), "OPPORTUNITY": "320000.25", "CURRENCY_ID": "RUB",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, monotonic(), payload))
        result: object
        if method == "crm.deal.list":
            if "@STAGE_ID" not in payload["filter"]:
                pytest.fail("Stage filter missing")
            assert payload["filter"]["CLOSED"] == "N"
            result = [deal] if len([name for name, _, _ in calls if name == method]) == 1 else []
        elif method == "crm.deal.get":
            result = {"CLOSED": "N", "ASSIGNED_BY_ID": str(owner)}
        elif method == "crm.timeline.comment.list":
            assert payload["filter"] == {"ENTITY_ID": 94, "ENTITY_TYPE": "deal"}
            result = comments
        elif method == "crm.activity.list":
            assert payload["filter"] == {"OWNER_TYPE_ID": 2, "OWNER_ID": 94}
            result = [{"SUBJECT": "спецификация проекта", "COMPLETED": "Y", "END_TIME": DATE}]
        elif method == "crm.timeline.comment.add":
            assert "Риск:" in payload["fields"]["COMMENT"]
            result = 32
            comments.append({"ID": "32", "COMMENT": payload["fields"]["COMMENT"]})
        elif method == "tasks.task.add":
            fields = payload["fields"]
            assert fields["UF_CRM_TASK"] == ["D_94"]
            assert fields["RESPONSIBLE_ID"] == owner
            if not any(name == method for name, _, _ in calls[:-1]):
                return httpx.Response(429)
            tasks.append({"id": "91", "description": fields["DESCRIPTION"]})
            if ambiguous_status is not None:
                return httpx.Response(ambiguous_status)
            raise httpx.ReadTimeout("Lost task response", request=request)
        elif method == "tasks.task.list":
            assert payload["filter"]["UF_CRM_TASK"] == ["D_94"]
            first_lookup = sum(name == method for name, _, _ in calls) == 1
            result = {"tasks": [] if ambiguous_status is None and first_lookup else tasks}
        else:
            pytest.fail(f"Unexpected method {method}")
        return httpx.Response(200, json={"result": result})

    config = Settings(
        bitrix_mode="live", llm_mode="mock", reviver_variant="variant_b", database_path=tmp_path / "live.db",
        bitrix_webhook_url=SecretStr("https://test.invalid/rest/1/token"),
        bitrix_approver_tokens={7: SecretStr("manager"), 8: SecretStr("other"), 9: SecretStr("supervisor")},
        bitrix_supervisor_ids={9},
    )
    app = create_app(config, bitrix_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        scan = client.post(f"{PREFIX}/scan", headers={"Idempotency-Key": "initial"},
                           json={"stages": ["PROPOSAL"], "limit": 1})
        scan.raise_for_status()
        item = scan.json()["decisions"][0]
        assert item["should_revive"] and "спецификация проекта" in item["draft_message"]
        assert not tasks
        path = f"{PREFIX}/approve/{item['revival_id']}"
        assert client.post(path, json={"approve": True}).status_code == 403
        assert client.post(path, json={"approve": True, "approver_user_id": 7},
                           headers={"X-Approval-Token": "other"}).status_code == 403
        assert client.post(path, json={"approve": True, "approver_user_id": 8},
                           headers={"X-Approval-Token": "other"}).status_code == 403
        owner = 8
        assert client.post(path, json={"approve": False, "approver_user_id": 7},
                           headers={"X-Approval-Token": "manager"}).status_code == 403
        approved = client.post(path, json={"approve": True, "approver_user_id": 9},
                               headers={"X-Approval-Token": "supervisor"})
        approved.raise_for_status()
        assert approved.json()["approver_user_id"] == 9
        assert tasks[0]["description"].startswith(item["draft_message"] + "\n")
        assert client.post(f"{PREFIX}/reconcile").json() == {"completed_steps": 0}
        assert client.post(f"{PREFIX}/reconcile").json() == {"completed_steps": int(ambiguous_status is None)}
        assert client.post(f"{PREFIX}/reconcile").json() == {"completed_steps": 0}
        assert len(tasks) == 1
        assert client.get(f"{PREFIX}/queue").json()[0]["bitrix_sync_status"] == "synced"
        second = client.post(f"{PREFIX}/scan", headers={"Idempotency-Key": "next"},
                             json={"stages": ["PROPOSAL"]})
        second.raise_for_status()
        assert second.json()["decisions"] == []
    deal_filters = [payload["filter"] for name, _, payload in calls if name == "crm.deal.list"]
    assert deal_filters[1][">ID"] == 94 and deal_filters[1]["=DATE_MODIFY"] == DATE
    assert deal_filters[2][">DATE_MODIFY"] == DATE
    assert all(second[1] - first[1] >= 0.5 for first, second in pairwise(calls))
    with Database(config.database_path).connect() as connection:
        rows = connection.execute("SELECT receipt FROM bitrix_steps WHERE revival_id IS NOT NULL ORDER BY rowid")
        assert [json.loads(row["receipt"]) for row in rows] == [{"comment_id": 32}, {"task_id": 91}]
        assert connection.execute("SELECT COUNT(*) FROM touch_history WHERE deal_id=94").fetchone()[0] == 1


def test_commercial_promise_in_next_action_blocks_publication(tmp_path: Path) -> None:
    def completion(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 85, "completion_tokens": 60},
            "choices": [{"message": {"content": json.dumps({
                "stall_reason_category": "no_response",
                "stall_summary": "Ответ пока не получен",
                "draft_message": "Добрый день! Удобно обсудить следующий шаг?",
                "next_best_action": "Пообещать индивидуальный дисконт",
            })}}],
        })

    settings = Settings(
        database_path=tmp_path / "policy.db", bitrix_mode="mock", llm_mode="live",
        llm_rub_per_million_input_tokens=Decimal(0), llm_rub_per_million_output_tokens=Decimal(0),
        reviver_variant="variant_b",
    )
    with TestClient(create_app(settings, llm_transport=httpx.MockTransport(completion))) as client:
        response = client.post(f"{PREFIX}/scan", headers={"Idempotency-Key": "commercial"}, json={"limit": 1})
        response.raise_for_status()
        decision = response.json()["decisions"][0]
        assert decision["skip_reason"] == "commercial_policy"
        assert not decision["should_revive"] and decision["need_human"]
        assert client.get(f"{PREFIX}/queue").json() == []
