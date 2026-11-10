"""Thin, testable wrapper around the Anthropic SDK.

* One place for model, effort, prompt caching, server-side refusal fallback, retries (SDK) and cost accounting.
* `call_raw` is thread-safe (no DB access); the caller records usage from the main thread.
* Structured outputs use `output_config.format` (json_schema) and are re-validated with pydantic.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

# $/MTok: input, output, cache read, cache write (5-minute). Source: Claude API model table (Opus 5.5).
PRICES = {"claude-opus-5-5": (4.00, 20.00, 0.20, 5.00), "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125), "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00)}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(Exception):
    pass


class Refused(Exception):
    """The model declined (stop_reason == 'refusal'); caller falls back to the deterministic narrative."""


class Truncated(Exception):
    pass


@dataclass
class Result:
    resp: Any
    usage: dict
    usd: float

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.resp.content if getattr(b, "type", "") == "text")


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic -> JSON schema acceptable to structured outputs: inline $defs, additionalProperties:false,
    every property required, no unsupported keywords."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    strip = ("title", "default", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "format")

    def walk(n, props: bool = False):
        if isinstance(n, list):
            return [walk(x) for x in n]
        if not isinstance(n, dict):
            return n
        if props:                       # a {property_name: schema} map: never strip a property that is *named* 'title'
            return {k: walk(v) for k, v in n.items()}
        if "$ref" in n:
            return walk(copy.deepcopy(defs[n["$ref"].split("/")[-1]]))
        n = {k: v for k, v in n.items() if k not in strip}
        if n.get("type") == "object":
            n["additionalProperties"] = False
            n["required"] = list(n.get("properties", {}))
        return {k: walk(v, props=(k == "properties")) for k, v in n.items()}

    return walk(schema)


def cost_usd(model: str, u: dict) -> float:
    pi, po, pcr, pcw = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (u.get("input_tokens", 0) * pi + u.get("output_tokens", 0) * po
            + u.get("cache_read_input_tokens", 0) * pcr + u.get("cache_creation_input_tokens", 0) * pcw) / 1e6


def usage_dict(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {k: (getattr(u, k, 0) or 0) for k in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")} if u else {}


class LLM:
    def __init__(self, sdk, model: str = "claude-opus-5-5", use_fallbacks: bool = True):
        self.sdk, self.model, self.use_fallbacks = sdk, model, use_fallbacks

    def call_raw(self, *, system: str, messages: list[dict], effort: str, schema: type[BaseModel] | None = None,
                 tools: list[dict] | None = None, max_tokens: int = 16000) -> Result:
        kw: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens, messages=messages,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort},
        )
        if schema is not None:
            kw["output_config"]["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        if tools:
            kw["tools"] = tools
        if self.use_fallbacks:
            resp = self.sdk.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
        else:
            resp = self.sdk.messages.create(**kw)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise Refused(getattr(getattr(resp, "stop_details", None), "category", None) or "refusal")
        if getattr(resp, "stop_reason", None) == "max_tokens" and not tools:
            raise Truncated("max_tokens reached before the structured answer finished")
        u = usage_dict(resp)
        return Result(resp, u, cost_usd(self.model, u))

    def parse(self, result: Result, model: type[BaseModel]):
        return model.model_validate(json.loads(result.text))
