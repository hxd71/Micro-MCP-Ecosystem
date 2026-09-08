"""Multi-provider LLM client for error attribution.

Supports OpenAI, Anthropic, Ollama, and any OpenAI-compatible endpoint.
The client is designed to fail gracefully: if no provider is configured
or a call fails, the deterministic pipeline continues without LLM enrichment.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .llm import LLMConfig, LLMProvider
from .models import LLMAttribution

_SYSTEM_PROMPT = (
    "You are a terminal and code error analysis assistant. "
    "Given error text, environment context, rule-based findings, and any prior knowledge, "
    "produce a structured JSON attribution with these keys:\n"
    "- primary_cause: a concise explanation of the most likely root cause\n"
    "- confidence: a number between 0.0 and 1.0\n"
    "- remediation_steps: a short list of concrete remediation steps\n"
    "- proposed_command: a single safe read-only or verification command to run, or null\n"
    "- needs_approval: true if the proposed command may change state\n"
    "- safety_notes: a short list of safety caveats\n"
    "Do not include markdown formatting; return only valid JSON."
)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a possibly chatty LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


class LLMClient:
    """Multi-provider LLM client with automatic API format adaptation."""

    def __init__(self, config: LLMConfig):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.is_configured

    async def _chat_openai(self, messages: list[dict[str, str]]) -> str:
        """OpenAI-compatible /chat/completions endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.extra_headers:
            headers.update(self.config.extra_headers)

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return str(data["choices"][0]["message"]["content"])

    async def _chat_anthropic(self, messages: list[dict[str, str]]) -> str:
        """Anthropic /messages endpoint."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }
        if self.config.extra_headers:
            headers.update(self.config.extra_headers)

        # Anthropic uses a different message format: system is separate
        system_msg = ""
        anthropic_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                anthropic_messages.append({"role": msg["role"], "content": msg["content"]})

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": anthropic_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_msg:
            payload["system"] = system_msg

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return str(data["content"][0]["text"])

    async def _chat(self, messages: list[dict[str, str]]) -> str:
        """Route to the appropriate provider's chat method."""
        if self.config.provider == LLMProvider.ANTHROPIC:
            return await self._chat_anthropic(messages)
        # OpenAI, Ollama, and OpenAI-compatible all use the same format
        return await self._chat_openai(messages)

    async def attribute_error(
        self,
        *,
        text: str,
        command: str,
        language: str,
        exit_code: int | None,
        env: dict[str, Any],
        findings: list[dict[str, Any]],
        retrieved_chunks: list[dict[str, Any]],
    ) -> LLMAttribution | None:
        """Ask the LLM for structured error attribution."""
        if not self.enabled:
            return None

        user_prompt = {
            "error_text": text[:4000],
            "command": command,
            "language": language,
            "exit_code": exit_code,
            "environment": env,
            "rule_findings": findings[:10],
            "retrieved_knowledge": [
                {"title": c.get("title"), "content": c.get("content")}
                for c in retrieved_chunks[:5]
            ],
        }
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False, default=str)},
        ]
        try:
            raw = await self._chat(messages)
            data = _extract_json(raw)
            return LLMAttribution.model_validate(data)
        except Exception:
            return None
