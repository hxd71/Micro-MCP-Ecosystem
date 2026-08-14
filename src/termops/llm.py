"""LLM configuration with multi-provider support.

Supports OpenAI, Anthropic, Ollama, and any OpenAI-compatible endpoint.
Each provider has its own API key, base URL, and model settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LLMProvider(Enum):
    """Supported LLM backends."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"  # vLLM, LiteLLM, LocalAI, etc.


PROVIDER_DEFAULTS: dict[LLMProvider, dict[str, str]] = {
    LLMProvider.OPENAI: {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "env_key": "OPENAI_API_KEY",
    },
    LLMProvider.ANTHROPIC: {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "env_key": "ANTHROPIC_API_KEY",
    },
    LLMProvider.OLLAMA: {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "env_key": "OLLAMA_API_KEY",
    },
    LLMProvider.OPENAI_COMPATIBLE: {
        "base_url": "http://localhost:8080/v1",
        "model": "",
        "env_key": "LLM_API_KEY",
    },
}


@dataclass
class LLMConfig:
    """Configuration for a single LLM provider connection."""

    provider: LLMProvider = LLMProvider.OLLAMA
    enabled: bool = False
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        defaults = PROVIDER_DEFAULTS.get(self.provider, {})
        if not self.base_url:
            self.base_url = defaults.get("base_url", "")
        if not self.model:
            self.model = defaults.get("model", "")

    @property
    def is_configured(self) -> bool:
        """True if the provider has enough config to attempt a call."""
        return self.enabled and bool(self.base_url) and bool(self.model)