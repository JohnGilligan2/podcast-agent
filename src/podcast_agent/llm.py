"""Thin wrapper around the Anthropic Messages API.

Keeping the API call behind one small interface (`LLMClient.create`) means the agent loop
can be unit-tested with a scripted fake, and the provider can be swapped (e.g. to Claude on
AWS Bedrock via `AnthropicBedrockMantle`) without touching agent logic.
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from .config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMClient(Protocol):
    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Any: ...


class ClaudeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        # The SDK retries 408/409/429/5xx and connection errors with exponential backoff.
        self.client = anthropic.Anthropic(max_retries=4, timeout=300.0)

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Any:
        params: dict[str, Any] = dict(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            # Adaptive thinking with summarised reasoning, so the logs show *why* each step happened.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.effort},
            # Automatic prompt caching: the growing conversation prefix is re-read at ~10% cost each turn.
            cache_control={"type": "ephemeral"},
        )
        if self.settings.refusal_fallback:
            # If a safety classifier declines, the server retries on a fallback model instead of failing.
            return self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.beta.messages.create(**params)
