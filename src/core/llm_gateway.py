import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel

from src.core.config import Settings
from src.core.text import tokens


class BudgetExceeded(Exception):
    pass


class InvalidCompletion(ValueError):
    pass


@dataclass
class CycleUsage:
    cost_rub: Decimal = Decimal(0)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_complete: bool = True

    spending_limit: Decimal | None = None
    on_change: Callable[["CycleUsage"], None] | None = None

    @property
    def rounded_cost(self) -> Decimal:
        return rubles(self.cost_rub)

    def mark(self, *, complete: bool) -> None:
        self.cost_complete = complete
        if self.on_change is not None:
            self.on_change(self)

    def authorize(self, limit: Decimal | None, estimate: Decimal | None) -> None:
        if self.spending_limit is not None and limit is not None:
            limit = min(self.spending_limit, limit)
        if not self.cost_complete or limit is None or estimate is None or self.cost_rub + estimate > limit:
            raise BudgetExceeded


def rubles(cost: Decimal) -> Decimal:
    return cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def unit_vector(vector: list[float]) -> list[float]:
    length = math.hypot(*vector)
    if not length or not math.isfinite(length):
        raise InvalidCompletion("Invalid embedding vector")
    return [value / length for value in vector]


class LLMGateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @staticmethod
    def load_prompt(path: Path, variant: str, *, include_base: bool = True) -> str:
        prompt = yaml.safe_load(path.read_text(encoding="utf-8"))
        base = prompt["base"] + "\n" if include_base else ""
        return str(base + prompt["variants"][variant])

    async def embed(self, text: str, usage: CycleUsage) -> list[float]:
        settings = self.settings
        if settings.llm_mode == "mock":
            vector = [0.0] * 512
            for word in tokens(text):
                digest = hashlib.blake2b(word.encode("utf-8"), digest_size=4).digest()
                vector[int.from_bytes(digest, "big") % len(vector)] += 1
            return unit_vector(vector) if any(vector) else vector
        rate = settings.embedding_rub_per_million_input_tokens
        input_bound = len(text.encode("utf-8")) + 1024
        usage.authorize(
            settings.max_cycle_cost_rub, None if rate is None else input_bound * rate / 1_000_000,
        )
        assert rate is not None
        usage.mark(complete=False)
        response = await self.client.post(
            settings.base_url.rstrip("/") + "/embeddings",
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            json={"model": settings.embedding_model, "input": text},
        )
        response.raise_for_status()
        data = response.json()
        tokens_in = data["usage"]["prompt_tokens"]
        if type(tokens_in) is not int or tokens_in < 0:
            raise ValueError("Invalid embedding token usage")
        usage.tokens_in += tokens_in
        usage.cost_rub += tokens_in * rate / 1_000_000
        usage.mark(complete=True)
        usage.authorize(settings.max_cycle_cost_rub, Decimal(0))
        vector = data["data"][0]["embedding"]
        if not isinstance(vector, list) or not vector:
            raise InvalidCompletion("Expected an embedding vector")
        return unit_vector([float(component) for component in vector])

    async def evaluate(
        self, system: str, payload: str, usage: CycleUsage, *, model: Literal["fast", "smart"] = "smart",
    ) -> dict[str, Any]:
        settings = self.settings
        rate_in = settings.llm_rub_per_million_input_tokens
        rate_out = settings.llm_rub_per_million_output_tokens
        output_limit = 2048
        messages = [{"role": "system", "content": system}, {"role": "user", "content": payload}]
        input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 1024
        estimate = (
            None if rate_in is None or rate_out is None
            else (input_bound * rate_in + output_limit * rate_out) / 1_000_000
        )
        usage.authorize(settings.max_cycle_cost_rub, estimate)
        assert rate_in is not None and rate_out is not None
        usage.mark(complete=False)
        response = await self.client.post(
            settings.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            json={
                "model": settings.fast_llm_model if model == "fast" else settings.smart_llm_model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": output_limit,
            },
        )
        response.raise_for_status()
        data = response.json()
        tokens_in = data["usage"]["prompt_tokens"]
        tokens_out = data["usage"]["completion_tokens"]
        if any(type(count) is not int or count < 0 for count in (tokens_in, tokens_out)):
            raise ValueError("Invalid token usage")
        usage.tokens_in += tokens_in
        usage.tokens_out += tokens_out
        usage.cost_rub += (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
        usage.mark(complete=True)
        usage.authorize(settings.max_cycle_cost_rub, Decimal(0))
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise InvalidCompletion("completion exceeded the output limit")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise InvalidCompletion("completion content must be a JSON object")
            return clean_json(content)
        except InvalidCompletion:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise InvalidCompletion("completion must contain a complete JSON object") from None


def clean_json(content: str) -> dict[str, Any]:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content.strip(), re.IGNORECASE | re.DOTALL)
    parsed = json.loads(match[1] if match else content)
    if not isinstance(parsed, dict):
        raise TypeError("Expected JSON object")
    return parsed


def known_fields(data: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key in schema.model_fields}

