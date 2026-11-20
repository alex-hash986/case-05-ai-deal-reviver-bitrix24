from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from streamlit.testing.v1 import AppTest

from src.api.main import create_app
from src.core.config import ROOT, Settings
from src.core.llm_gateway import CycleUsage
from src.domain.guardrails import CommercialPolicyGuard, eligibility_reason
from src.domain.schemas import DemoFixture, ScanIn, StageSLA
from src.domain.scoring import score_deal
from src.domain.service import Reviver

PREFIX = "/api/v1/reviver"


def scan(client: TestClient, key: str, **payload: Any) -> httpx.Response:
    return cast(httpx.Response, client.post(f"{PREFIX}/scan", json=payload, headers={"Idempotency-Key": key}))


def demo_deal() -> DemoFixture:
    return TypeAdapter(list[DemoFixture]).validate_json((ROOT / "data/demo_deals.json").read_bytes())[0]


def test_decimal_scoring_and_eligibility() -> None:
    deal = demo_deal().deal
    sla = StageSLA(days=7, base_probability=Decimal("0.35"))
    score = score_deal(deal, sla)
    assert score.sla_excess_days == 12
    assert score.stagnation_score == Decimal(78)
    assert score.revival_probability == Decimal("0.2135")
    assert score.at_risk_revenue_rub == Decimal("1131000.39")
    assert score.expected_recovered_rub == Decimal("241468.58")
    now = datetime.now(UTC)
    assert eligibility_reason(deal, sla, now, now - timedelta(days=13), 14) == "cooldown"
    assert eligibility_reason(deal, sla, now, now - timedelta(days=14), 14) is None
    at_boundary = deal.model_copy(update={"days_in_stage": 7})
    assert eligibility_reason(at_boundary, sla, now, None, 14) == "within_sla"
    with pytest.raises(ValidationError):
        ScanIn.model_validate({"unexpected": True})


@pytest.mark.parametrize("message", [
    "Предлагаем скидку ５％.", "Подарок при заказе.", "Бесплатно.", "У нас акция.",
    "Бонус на первую покупку.", "Гарантируем срок до пятницы.", "Предлагаем рассрочку.",
])
def test_commercial_guard_blocks_unsupported_offers(message: str) -> None:
    import yaml

    guard = CommercialPolicyGuard.model_validate(
        yaml.safe_load((ROOT / "config/commercial_policy.yaml").read_text(encoding="utf-8")),
    )
    deal = demo_deal().deal
    assert not guard.allows(message, deal)
    assert guard.allows("Добрый день! Актуален ли ещё ваш запрос?", deal)


def test_commercial_guard_requires_both_sources() -> None:
    guard = CommercialPolicyGuard(restricted_pattern=r"%|скид", approved_terms=["Скидка 5%"])
    deal = demo_deal().deal
    assert not guard.allows("Скидка 5%.", deal)
    deal.verified_commercial_terms = ["Скидка 5%"]
    assert guard.allows("Скидка 5%.", deal)
    assert not guard.allows("Скидка 5% и дополнительно 10%.", deal)
    assert not guard.allows("Каждому клиенту скидка 5%.", deal)


def test_api_queue_and_reconcile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_path=tmp_path / "api.db", llm_mode="mock")
    app = create_app(settings)
    with TestClient(app) as client:
        service: Reviver = app.state.reviver
        publish = service.bitrix.publish
        interrupted = False

        async def lose_receipt(operation: str, method: str, payload: Any) -> str:
            nonlocal interrupted
            result = await publish(operation, method, payload)
            if not interrupted:
                interrupted = True
                raise httpx.ReadTimeout("Commit succeeded before timeout")
            return result

        monkeypatch.setattr(service.bitrix, "publish", lose_receipt)
        assert client.get("/health").json() == {"status": "ok", "database": "wal"}
        assert client.post(f"{PREFIX}/scan", json={}).status_code == 422
        response = scan(client, "first")
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["decisions"]) == 12
        assert scan(client, "first").json() == data
        assert scan(client, "first", limit=1).status_code == 409
        assert {item["skip_reason"] for item in data["decisions"]} == {
            None, "within_sla", "cooldown", "future_activity", "customer_declined", "insufficient_history",
        }
        assert scan(client, "next").json()["decisions"] == []
        contract = scan(client, "contract", stages=["CONTRACT"], limit=1).json()
        assert contract["decisions"][0]["deal_id"] == 502
        assert scan(client, "contract-next", stages=["CONTRACT"], limit=1).json()["decisions"][0]["deal_id"] == 505
        assert client.post(f"{PREFIX}/reconcile").json() == {"completed_steps": 0}
        queue = client.get(f"{PREFIX}/queue").json()
        assert len(queue) == 5
        assert {item["bitrix_sync_status"] for item in queue} == {"synced"}
        path = f"{PREFIX}/approve/{queue[0]['revival_id']}"
        other = f"{PREFIX}/approve/{queue[1]['revival_id']}"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 10
            assert conn.execute("SELECT MAX(attempts) FROM bitrix_steps").fetchone()[0] == 2
        approved = client.post(path, json={"approve": True}).json()
        assert approved["approval_status"] == "approved"
        assert approved["bitrix_sync_status"] == "synced"
        assert client.post(path, json={"approve": True}).json() == approved
        assert client.post(path, json={"approve": False}).status_code == 409
        assert client.post(other, json={"approve": False}).json()["approval_status"] == "rejected"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 11
        metrics = client.get("/api/v1/metrics").json()
        assert Decimal(metrics["approval_rate"]) == Decimal("0.2")
        assert Decimal(metrics["at_risk_revenue_rub"]) > 0
        assert metrics["queue_size"] == 3


def test_budget_stop_and_exact_cost(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def completion(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            "choices": [{"message": {"content": (
                '{"stall_reason_category":"no_response","stall_summary":"Клиент без ответа",'
                '"draft_message":"Добрый день! Актуален ли ваш запрос?",'
                '"next_best_action":"Уточнить приоритет","extra":"ignored"}'
            )}}],
        })

    settings = Settings(
        database_path=tmp_path / "budget.db", llm_mode="live",
        llm_rub_per_million_input_tokens=Decimal(10),
        llm_rub_per_million_output_tokens=Decimal(10),
        max_cycle_cost_rub=Decimal("0.002"),
        max_session_cost_rub=Decimal("0.06"),
    )
    transport = httpx.MockTransport(completion)
    with TestClient(create_app(settings, llm_transport=transport)) as client:
        item = scan(client, "blocked", limit=1).json()["decisions"][0]
        assert item["skip_reason"] == "budget_exhausted"
        assert calls == []
        settings.max_cycle_cost_rub = Decimal(1)
        item = scan(client, "allowed", limit=1).json()["decisions"][0]
        assert item["should_revive"]
        assert Decimal(item["cost_rub"]) == Decimal("0.01")
        settings.max_session_cost_rub = Decimal("0.011")
        item = scan(client, "session-blocked", limit=1).json()["decisions"][0]
        assert item["skip_reason"] == "budget_exhausted"
        assert len(calls) == 1
    assert CycleUsage(cost_rub=Decimal("0.005")).rounded_cost == Decimal("0.01")


def test_streamlit_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_path=tmp_path / "ui.db", llm_mode="mock")
    with TestClient(create_app(settings)) as client:
        def local_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            kwargs.pop("timeout", None)
            return cast(httpx.Response, client.request(method, httpx.URL(url).path, **kwargs))

        monkeypatch.setattr(httpx, "request", local_request)
        demo = AppTest.from_file(str(ROOT / "src/ui/app.py"), default_timeout=15).run()
        assert not demo.exception
        assert [tab.label for tab in demo.tabs] == ["Live Demo & Queue", "Executive ROI-дашборд"]
        demo.number_input[0].set_value(1)
        next(button for button in demo.button if button.label == "Сканировать воронку").click().run()
        assert not demo.exception and not demo.error
        next(button for button in demo.button if button.label == "Одобрить и поставить в работу").click().run()
        assert not demo.exception and not demo.error
        assert "Одобрено" in demo.expander[0].label
        assert client.get("/api/v1/metrics").json()["queue_size"] == 0
