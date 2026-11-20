import asyncio
import os
from time import perf_counter

import httpx
from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, ValidationError

from src.core.config import ROOT, Settings
from src.core.llm_gateway import BudgetExceeded, CycleUsage, LLMGateway

ALIASES = {
    "base_url": "LLM_BASE_URL",
    "api_key": "LLM_API_KEY",
    "fast_llm_model": "LLM_MODEL_FAST",
    "smart_llm_model": "LLM_MODEL_SMART",
}
REQUIRED = (
    *ALIASES, "llm_rub_per_million_input_tokens", "llm_rub_per_million_output_tokens",
    "max_cycle_cost_rub", "max_session_cost_rub",
)
BITRIX_METHODS = ("crm.deal.list", "crm.timeline.comment.list", "crm.activity.list", "tasks.task.add")


class LiveSettings(Settings):
    base_url: str = Field(validation_alias=AliasChoices("BASE_URL", "LLM_BASE_URL"))
    api_key: SecretStr = Field(validation_alias=AliasChoices("API_KEY", "LLM_API_KEY"))
    fast_llm_model: str = Field(validation_alias=AliasChoices("FAST_LLM_MODEL", "LLM_MODEL_FAST"))
    smart_llm_model: str = Field(validation_alias=AliasChoices("SMART_LLM_MODEL", "LLM_MODEL_SMART"))


class PreflightError(Exception):
    pass


def load_settings() -> Settings:
    explicit = {key.upper(): value for key, value in dotenv_values(ROOT / ".env").items()}
    explicit.update({key.upper(): value for key, value in os.environ.items()})
    missing = [
        field.upper() for field in REQUIRED
        if not (explicit.get(field.upper()) or explicit.get(ALIASES.get(field, "")))
    ]
    if missing:
        raise PreflightError("missing explicit settings: " + ", ".join(missing))
    try:
        settings = LiveSettings(llm_mode="live", _env_file=ROOT / ".env")
    except ValidationError as error:
        # Validation errors can contain the original value, including credentials.
        raise PreflightError(f"settings validation failed ({error.error_count()} fields)") from None
    except ValueError:
        raise PreflightError("settings could not be parsed") from None
    url = httpx.URL(settings.base_url)
    local = url.host == "127.0.0.1"
    if (
        not url.host or url.userinfo or url.query or url.fragment
        or (url.scheme != "https" and not (local and url.scheme == "http"))
        or url.host == "localhost"
    ):
        raise PreflightError("BASE_URL requires HTTPS or HTTP on 127.0.0.1")
    for name, value in (
        ("API_KEY", settings.api_key.get_secret_value()),
        ("FAST_LLM_MODEL", settings.fast_llm_model),
        ("SMART_LLM_MODEL", settings.smart_llm_model),
    ):
        if not value or any(not 33 <= ord(char) <= 126 for char in value):
            raise PreflightError(f"{name} must be nonempty printable ASCII without spaces")
    if not local and settings.api_key.get_secret_value().lower() in {"ollama", "changeme", "placeholder", "your-api-key"}:
        raise PreflightError("API_KEY is a placeholder for an external gateway")
    return settings


def diagnostic(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = f"HTTP {error.response.status_code}"
        try:
            body = error.response.json()
        except ValueError:
            return status
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = body["error"]
            message = str(detail.get("message", "")).lower()
            for marker, reason in (
                ("not a valid model", "provider rejected the model"),
                ("no endpoints found", "provider has no endpoint for the request"),
                ("response_format", "provider reported an issue with response_format"),
                ("max_tokens", "provider reported an issue with max_tokens"),
                ("model_not_found", "provider could not find the model"),
                ("insufficient", "provider reported insufficient capacity or quota"),
            ):
                if marker in message or marker == detail.get("code"):
                    return f"{status} ({reason})"
        return status
    if isinstance(error, BudgetExceeded):
        return "budget cannot authorize the request or actual usage exceeded the budget"
    return type(error).__name__


async def probe_llm(settings: Settings, client: httpx.AsyncClient) -> CycleUsage:
    usage = CycleUsage(spending_limit=settings.max_session_cost_rub)
    started = perf_counter()
    try:
        gateway = LLMGateway(settings, client)
        prompt = gateway.load_prompt(ROOT / "config/revival_prompt.yaml", "preflight_v1", include_base=False)
        result = await gateway.evaluate(prompt, "{}", usage)
        if result:
            raise ValueError("Expected an empty JSON object")
    except (BudgetExceeded, httpx.HTTPError, ValueError, TypeError, KeyError, httpx.InvalidURL) as error:
        raise PreflightError(
            f"{diagnostic(error)}; cost_rub={usage.rounded_cost}; cost_complete={usage.cost_complete}",
        ) from None
    print(
        f"LLM_LIVE_OK: latency_ms={(perf_counter() - started) * 1000:.3f}; "
        f"tokens_in={usage.tokens_in}; tokens_out={usage.tokens_out}; "
        f"cost_rub={usage.rounded_cost}; cost_rub_exact={usage.cost_rub}",
    )
    return usage


async def probe_bitrix(settings: Settings, client: httpx.AsyncClient) -> bool:
    raw = settings.bitrix_webhook_url.get_secret_value().strip() if settings.bitrix_webhook_url else ""
    url = httpx.URL(raw)
    if (
        raw.lower() in {"", "placeholder", "changeme"}
        or url.host in {"example.com", "example.org", "example.net", "portal.example"}
        or url.host.endswith((".example", ".invalid", ".example.com"))
        or any(marker in raw.lower() for marker in ("webhook-secret", "your_webhook", "placeholder"))
    ):
        print("BITRIX_LIVE_SKIPPED: webhook not configured")
        return True
    parts = url.path.strip("/").split("/")
    if (
        url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
        or len(parts) != 3 or parts[0] != "rest" or not parts[1].isdigit() or not parts[2]
    ):
        print("BITRIX_LIVE_FAILED: invalid HTTPS webhook base URL")
        return False
    passed = True
    for index, method in enumerate(BITRIX_METHODS):
        if index:
            await asyncio.sleep(0.5)
        try:
            response = await client.post(f"{str(url).rstrip('/')}/method.get", json={"name": method})
            response.raise_for_status()
            result = response.json()["result"]
            if result["isExisting"] is not True or result["isAvailable"] is not True:
                raise ValueError("Unavailable method")
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            passed = False
            print(f"BITRIX_LIVE_FAILED: {method}; {diagnostic(error)}")
        else:
            print(f"BITRIX_SCOPE_OK: {method}")
    return passed


async def preflight() -> tuple[Settings, CycleUsage]:
    try:
        settings = load_settings()
    except httpx.InvalidURL:
        raise PreflightError("invalid LLM base URL") from None
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            bitrix_ok = await probe_bitrix(settings, client)
        except httpx.InvalidURL:
            print("BITRIX_LIVE_FAILED: invalid HTTPS webhook base URL")
            bitrix_ok = False
        usage = await probe_llm(settings, client)
    if not bitrix_ok:
        raise PreflightError("Bitrix24 method availability check failed")
    return settings, usage


if __name__ == "__main__":
    try:
        asyncio.run(preflight())
    except PreflightError as error:
        raise SystemExit(f"LIVE_PREFLIGHT_FAILED: {error}") from None
