"""LLM provider abstraction — Anthropic, Cerebras, or any OpenAI-compatible API."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ProviderKind(str, Enum):
    ANTHROPIC = "anthropic"
    CEREBRAS = "cerebras"
    OPENAI_COMPAT = "openai_compat"  # vLLM, Together, Groq, etc.


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Configuration for a single LLM provider."""

    kind: ProviderKind
    model: str
    api_key_env: str  # env var name, never the raw key
    base_url: str | None = None  # override for OpenAI-compat endpoints
    max_tokens: int = 16_384
    temperature: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        val = os.environ.get(self.api_key_env, "")
        if not val:
            raise EnvironmentError(
                f"Missing API key: set ${self.api_key_env}"
            )
        return val


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens
    raw: Any = None  # provider-specific response object


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Completion: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULTS: dict[ProviderKind, dict[str, str]] = {
    ProviderKind.ANTHROPIC: {
        "model": "claude-sonnet-4-20250514",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    ProviderKind.CEREBRAS: {
        "model": "llama-4-scout-17b-16e-instruct",
        "api_key_env": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
    },
    ProviderKind.OPENAI_COMPAT: {
        "model": "default",
        "api_key_env": "OPENAI_API_KEY",
    },
}


def make_provider_config(
    kind: ProviderKind | str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    **kwargs: Any,
) -> ProviderConfig:
    """Build a ProviderConfig with sensible defaults per provider."""
    if isinstance(kind, str):
        kind = ProviderKind(kind)
    defaults = _PROVIDER_DEFAULTS.get(kind, {})
    return ProviderConfig(
        kind=kind,
        model=model or defaults.get("model", "default"),
        api_key_env=api_key_env or defaults.get("api_key_env", "API_KEY"),
        base_url=base_url or defaults.get("base_url"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Concrete providers (lazy imports so deps are optional)
# ---------------------------------------------------------------------------

class AnthropicProvider:
    """Anthropic Messages API."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.config.api_key)
        return self._client

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Completion:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature if temperature is not None else self.config.temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        resp = await client.messages.create(**kwargs)
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text += block.text
        return Completion(
            text=text,
            usage={
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            },
            raw=resp,
        )


class CerebrasProvider:
    """Cerebras Cloud — OpenAI-compatible chat endpoint, optimized for speed."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url or "https://api.cerebras.ai/v1",
            )
        return self._client

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Completion:
        client = self._get_client()
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        return Completion(
            text=choice.message.content or "",
            usage={
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            } if resp.usage else {},
            raw=resp,
        )


class OpenAICompatProvider(CerebrasProvider):
    """Any OpenAI-compatible endpoint (vLLM, Together, Groq, etc.)."""

    def _get_client(self) -> Any:
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        return self._client


def create_provider(config: ProviderConfig) -> LLMProvider:
    """Instantiate the right provider from config."""
    match config.kind:
        case ProviderKind.ANTHROPIC:
            return AnthropicProvider(config)
        case ProviderKind.CEREBRAS:
            return CerebrasProvider(config)
        case ProviderKind.OPENAI_COMPAT:
            return OpenAICompatProvider(config)
        case _:
            raise ValueError(f"Unknown provider: {config.kind}")
