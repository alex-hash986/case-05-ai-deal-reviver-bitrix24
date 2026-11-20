from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Activity(DTO):
    text: str
    due_at: AwareDatetime | None = None
    completed: bool = True


class DealSnapshot(DTO):
    deal_id: int = Field(gt=0)
    title: str
    stage_id: str
    stage_name: str
    amount_rub: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    assigned_by_id: int = Field(gt=0)
    assigned_by_name: str
    last_activity_at: AwareDatetime
    days_in_stage: int = Field(ge=0)
    days_since_last_touch: int = Field(ge=0)
    comments_history: list[str]
    activities_history: list[Activity]
    verified_commercial_terms: list[str] = Field(default_factory=list)


class Draft(DTO):
    stall_reason_category: Literal["no_response", "internal_review", "budget", "negative", "unknown"]
    stall_summary: str = Field(min_length=1, max_length=1000)
    draft_message: str = Field(min_length=1, max_length=1500)
    next_best_action: str = Field(min_length=1, max_length=500)


class RevivalDecisionOut(DTO):
    revival_id: str
    deal_id: int
    deal_title: str
    assigned_by_name: str
    approver_user_id: int | None = None
    should_revive: bool = False
    skip_reason: str | None = None
    stagnation_score: Decimal = Field(Decimal(0), ge=0, le=100)
    revival_probability: Decimal = Field(Decimal(0), ge=0, le=1)
    at_risk_revenue_rub: Decimal = Field(Decimal(0), ge=0)
    expected_recovered_rub: Decimal = Field(Decimal(0), ge=0)
    stall_reason_category: str | None = None
    stall_summary: str | None = None
    draft_message: str | None = None
    next_best_action: str | None = None
    approval_status: Literal["pending_approval", "approved", "rejected", "skipped"] = "skipped"
    bitrix_sync_status: Literal["not_required", "pending", "synced"] = "not_required"
    cost_rub: Decimal = Decimal(0)
    latency_ms: int = 0
    need_human: bool = False


class ScanIn(DTO):
    stages: list[str] = Field(default_factory=list, max_length=30)
    limit: int = Field(12, ge=1, le=100)
    session_id: str = Field("local-demo", min_length=1, max_length=128)

    @field_validator("stages")
    @classmethod
    def normalize_stages(cls, stages: list[str]) -> list[str]:
        return sorted(set(stages))


class ScanOut(DTO):
    request_id: str
    next_cursor: str | None
    decisions: list[RevivalDecisionOut]


class ApprovalIn(DTO):
    approve: bool = Field(strict=True)
    approver_user_id: int | None = Field(None, strict=True, gt=0)


class StageSLA(DTO):
    days: int = Field(gt=0)
    base_probability: Decimal = Field(ge=0, le=1)


class DealPage(DTO):
    deals: list[DealSnapshot]
    next_cursor: str | None


class Metrics(DTO):
    at_risk_revenue_rub: Decimal
    expected_recovered_rub: Decimal
    approval_rate: Decimal
    average_latency_ms: Decimal
    cost_rub: Decimal
    cost_complete: bool
    queue_size: int
    scans: int


class DemoFixture(DTO):
    deal: DealSnapshot
    stage_age_days: int
    touch_age_days: int
    future_activity: bool = False
    cooldown_age_days: int | None = None
    updated_at: AwareDatetime
