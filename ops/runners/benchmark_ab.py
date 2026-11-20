import argparse
import asyncio
import json
import math
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx
from pydantic import AwareDatetime

from ops.runners.build_deals_60 import AS_OF
from ops.runners.preflight_live import PreflightError, preflight
from src.core.config import ROOT, Settings
from src.core.database import Database
from src.core.llm_gateway import LLMGateway, rubles
from src.domain.guardrails import normalize
from src.domain.schemas import DTO, DealSnapshot, DemoFixture, RevivalDecisionOut, ScanIn
from src.domain.service import Reviver
from src.integrations.bitrix_connector import MockBitrix

Variant = Literal["variant_a", "variant_b"]
Mode = Literal["mock", "live"]


class EvalCase(DTO):
    case_id: str
    category: Literal["revive", "eligibility_exclusion", "negative_exclusion", "commercial_policy_trap"]
    deal: DealSnapshot
    expected_should_revive: bool
    expected_skip_reason: str | None
    expected_stall_category: str | None
    forbidden_claims: list[str]
    historical_won: bool
    last_revival_at: AwareDatetime | None
    pending_approval: bool


class BenchmarkBitrix(MockBitrix):
    async def pace(self) -> None:
        return


def load_cases() -> list[EvalCase]:
    return [
        EvalCase.model_validate_json(line)
        for line in (ROOT / "data/eval/deals_60.jsonl").read_text(encoding="utf-8").splitlines()
    ]


async def evaluate(
    cases: list[EvalCase], directory: Path, variant: Variant, settings: Settings,
) -> dict[str, object]:
    settings = settings.model_copy(update={"bitrix_mode": "mock", "reviver_variant": variant})
    db = Database(directory / f"{variant}.db")
    fixtures = [
        DemoFixture(deal=case.deal, stage_age_days=case.deal.days_in_stage,
                    touch_age_days=case.deal.days_since_last_touch, updated_at=AS_OF).model_dump(mode="json")
        for case in cases
    ]
    fixture_path = directory / "fixtures.json"
    fixture_path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    bitrix = BenchmarkBitrix(db, fixture_path, AS_OF)
    with db.connect() as connection:
        for case in cases:
            deal = case.deal
            if case.last_revival_at:
                connection.execute("INSERT INTO touch_history VALUES (?,?,?)",
                                   (case.case_id, deal.deal_id, case.last_revival_at.isoformat()))
            if case.pending_approval:
                item = RevivalDecisionOut(
                    revival_id=case.case_id, deal_id=deal.deal_id, deal_title=deal.title,
                    assigned_by_name=deal.assigned_by_name, approval_status="pending_approval",
                )
                connection.execute("INSERT INTO revival_items VALUES (?,?,?,?,?)",
                                   (case.case_id, deal.deal_id, item.model_dump_json(),
                                    deal.title, deal.assigned_by_id))
    # One session preserves the configured session budget. Each case gets its own cycle cap.
    async with httpx.AsyncClient(timeout=30) as client:
        service = Reviver(settings, db, bitrix, LLMGateway(settings, client), clock=lambda: AS_OF)
        decisions = {}
        for index in range(len(cases)):
            scan = await service.scan(f"{variant}-{index}", ScanIn(limit=1, session_id=f"benchmark-{variant}"))
            item = scan.decisions[0]
            decisions[item.deal_id] = item

    matches = excluded = exclusions = classified = classified_correct = 0
    violations = offers = probes = blocked_probes = verified = 0
    forecast = actual = squared_error = Decimal(0)
    failures = []
    for case in cases:
        item = decisions[case.deal.deal_id]
        eligible = item.should_revive == case.expected_should_revive
        reason_matches = item.skip_reason == case.expected_skip_reason
        matches += eligible
        if not case.expected_should_revive:
            exclusions += 1
            excluded += eligible and reason_matches
        stall_matches = item.stall_reason_category == case.expected_stall_category
        if case.expected_stall_category is not None:
            classified += 1
            classified_correct += stall_matches
        if not (eligible and reason_matches and stall_matches):
            failures.append(case.case_id)
        if item.draft_message:
            offers += 1
            violations += (
                any(not service.policy.allows(text, case.deal) for text in
                    (item.draft_message, item.next_best_action or ""))
                or any(normalize(claim) in normalize(item.draft_message) for claim in case.forbidden_claims)
            )
        probes += len(case.forbidden_claims)
        blocked_probes += sum(not service.policy.allows(claim, case.deal) for claim in case.forbidden_claims)
        verified += sum(service.policy.allows(claim, case.deal) for claim in case.deal.verified_commercial_terms)
        predicted = item.expected_recovered_rub if item.should_revive else Decimal(0)
        observed = case.deal.amount_rub if case.historical_won else Decimal(0)
        forecast += predicted
        actual += observed
        squared_error += ((predicted - observed) / case.deal.amount_rub) ** 2

    with db.connect() as connection:
        runs = connection.execute("SELECT * FROM scan_runs").fetchall()
    exact_cost = sum((Decimal(row["cost_rub"]) for row in runs), Decimal(0))
    latency = sorted(round(row["processing_time_sec"] * 1000, 3) for row in runs)
    return {
        "cases": len(cases), "eligibility_accuracy": matches / len(cases),
        "opt_out_cooldown_recall": excluded / exclusions,
        "stall_reason_accuracy": classified_correct / classified,
        "commercial_policy_compliance_rate": (offers - violations) / offers if offers else None,
        "unauthorized_offer_rate": violations / offers if offers else None,
        "drafts_emitted": offers, "unauthorized_offers": violations,
        "policy_probes": probes, "policy_probes_blocked": blocked_probes, "verified_terms_allowed": verified,
        "forecast_calibration": {
            "expected_recovered_rub": str(forecast), "historical_won_rub": str(actual),
            "forecast_minus_observed_rub": str(forecast - actual),
            "mean_squared_normalized_revenue_error": str((squared_error / len(cases)).quantize(Decimal("0.0001"))),
        },
        "latency_p50_ms": latency[math.ceil(len(latency) * .50) - 1],
        "latency_p95_ms": latency[math.ceil(len(latency) * .95) - 1],
        "avg_cost_rub": str(exact_cost / len(cases)),
        "total_cost_rub": str(rubles(exact_cost)),
        "total_cost_rub_exact": str(exact_cost),
        "cost_complete": all(row["cost_complete"] for row in runs),
        "tokens_in": sum(row["tokens_in"] for row in runs),
        "tokens_out": sum(row["tokens_out"] for row in runs),
        "mismatched_cases": failures,
        "skip_counts": {
            reason: sum(item.skip_reason == reason for item in decisions.values())
            for reason in sorted({item.skip_reason for item in decisions.values() if item.skip_reason is not None})
        },
    }


async def run(mode: Mode, output_dir: Path) -> dict[str, object]:
    cases = load_cases()
    budget: dict[str, str] | None = None
    if mode == "live":
        settings, usage = await preflight()
        # Reserve an equal share for each arm, including when one arm exhausts its share.
        per_variant = (settings.max_session_cost_rub - usage.cost_rub) / 2
        if per_variant <= 0 and (
            settings.llm_rub_per_million_input_tokens or settings.llm_rub_per_million_output_tokens
        ):
            raise PreflightError("no budget remains for A/B after the probe")
        budget = {
            "session_limit_rub": str(settings.max_session_cost_rub),
            "preflight_cost_rub_exact": str(usage.cost_rub),
            "per_variant_limit_rub": str(per_variant),
        }
        settings = settings.model_copy(update={"max_session_cost_rub": per_variant})
    else:
        settings = Settings(llm_mode="mock")
    variants: dict[str, dict[str, object]] = {}
    with TemporaryDirectory() as tmp:
        for variant in ("variant_a", "variant_b"):
            variants[variant] = await evaluate(cases, Path(tmp), variant, settings)
            if mode == "live" and not variants[variant]["cost_complete"]:
                raise PreflightError(f"{variant}: usage is incomplete; A/B stopped and reports preserved")
    report: dict[str, object] = {"mode": mode, "synthetic_outcomes": True, "variants": variants}
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# A/B-бенчмарк Case 05", "",
        f"Режим LLM — `{mode}`. Корпус и исходы синтетические, замеры сделаны локальным раннером.",
        "Денежная ошибка сравнивает прогноз с полной суммой синтетически выигранных сделок.",
        "Задержка включает локальный цикл сканирования и сохранения, без задержек REST Bitrix24.",
        "Recall требует правильного исключения во всех 24 отрицательных контрольных случаях.", "",
    ]
    if budget is not None:
        report["live_budget"] = budget
        lines += [
            (f"Лимит сессии — {budget['session_limit_rub']} ₽. Запрос preflight обошёлся в "
             f"{budget['preflight_cost_rub_exact']} ₽. Лимит каждого варианта — "
             f"{budget['per_variant_limit_rub']} ₽."), "",
            "Бюджетные пропуски и ошибки LLM входят в расчёт точности на полном корпусе.", "",
        ]
    lines += ["| Метрика | variant_a | variant_b |", "| --- | ---: | ---: |"]
    for key in (
        "eligibility_accuracy", "opt_out_cooldown_recall", "stall_reason_accuracy",
        "commercial_policy_compliance_rate", "unauthorized_offer_rate", "policy_probes_blocked",
        "verified_terms_allowed", "drafts_emitted", "latency_p50_ms", "latency_p95_ms",
        "avg_cost_rub", "total_cost_rub", "total_cost_rub_exact", "cost_complete", "tokens_in", "tokens_out",
    ):
        lines.append(f"| {key} | {variants['variant_a'][key]} | {variants['variant_b'][key]} |")
    lines += ["", "| Вариант | Прогноз, ₽ | Синтетические выигрыши, ₽ | Ошибка, ₽ |",
              "| --- | ---: | ---: | ---: |"]
    for variant, results in variants.items():
        calibration = results["forecast_calibration"]
        assert isinstance(calibration, dict)
        lines.append(
            f"| {variant} | {calibration['expected_recovered_rub']} | {calibration['historical_won_rub']} | "
            f"{calibration['forecast_minus_observed_rub']} |"
        )
    if mode == "live":
        lines += ["", "### Пропуски по бюджету и ошибки LLM", ""]
        for variant, results in variants.items():
            skip_counts = results["skip_counts"]
            assert isinstance(skip_counts, dict)
            lines.append(
                f"- {variant}: бюджет — {skip_counts.get('budget_exhausted', 0)}, "
                f"ошибки LLM — {skip_counts.get('llm_unavailable', 0)}.",
            )
    for filename, contents in (
        ("benchmark_ab_report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n"),
        ("benchmark_ab_report.md", "\n".join(lines) + "\n"),
    ):
        path = output_dir / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare both Reviver strategies on the fixed corpus.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock-llm", action="store_true")
    modes.add_argument("--live-llm", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs")
    args = parser.parse_args()
    try:
        asyncio.run(run("live" if args.live_llm else "mock", args.output_dir))
    except PreflightError as error:
        raise SystemExit(f"LIVE_BENCHMARK_FAILED: {error}") from None


if __name__ == "__main__":
    main()
