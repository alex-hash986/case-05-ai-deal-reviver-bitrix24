from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="forbid",
    )

    api_port: int = Field(8015, ge=1, le=65535)
    ui_port: int = Field(8505, ge=1, le=65535)
    database_path: Path = ROOT / "data/case05_deal_reviver.db"
    max_cycle_cost_rub: Decimal = Field(Decimal("1.00"), ge=0)
    max_session_cost_rub: Decimal = Field(Decimal("5.00"), ge=0)
    bitrix_mode: Literal["mock", "live"] = "mock"
    bitrix_webhook_url: SecretStr | None = None
    bitrix_approver_tokens: dict[int, SecretStr] = Field(default_factory=dict)
    bitrix_supervisor_ids: set[int] = Field(default_factory=set)
    bitrix_verified_terms_field: str | None = None
    bitrix_initial_modified_since: str | None = None
    reviver_variant: Literal["variant_a", "variant_b"] = "variant_a"
    revival_cooldown_days: int = Field(14, ge=1)
    llm_mode: Literal["mock", "live"] = "mock"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: SecretStr = SecretStr("ollama")
    fast_llm_model: str = "qwen2.5:7b"
    smart_llm_model: str = "qwen2.5:32b"
    embedding_model: str = "bge-m3"
    embedding_rub_per_million_input_tokens: Decimal | None = Field(None, ge=0)
    llm_rub_per_million_input_tokens: Decimal | None = Field(None, ge=0)
    llm_rub_per_million_output_tokens: Decimal | None = Field(None, ge=0)

    @field_validator("bitrix_approver_tokens")
    @classmethod
    def validate_approver_tokens(cls, tokens: dict[int, SecretStr]) -> dict[int, SecretStr]:
        values = set()
        for user_id, secret in tokens.items():
            token = secret.get_secret_value()
            if user_id <= 0 or not 1 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
                raise ValueError("Approval tokens require a positive user ID and printable ASCII")
            values.add(token)
        if len(values) != len(tokens):
            raise ValueError("Approval tokens must be unique per user")
        return tokens

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings, env_settings, dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=ROOT / "config/settings.yaml"),
            file_secret_settings,
        )

