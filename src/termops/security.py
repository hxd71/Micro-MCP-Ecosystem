"""Security helpers for audit integrity, secret redaction, and tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

SECRET_MARKERS = ("token", "password", "passwd", "secret", "api_key", "apikey", "authorization", "cookie")
REDACTED = "[REDACTED]"


def is_secret_key(key: str) -> bool:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
    words = {item for item in re.split(r"[^a-z0-9]+", snake_case) if item}
    return any(
        marker in words or snake_case == marker or snake_case.endswith(f"_{marker}")
        for marker in SECRET_MARKERS
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def action_digest(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json(payload))


def redact(value: Any, key: str = "") -> Any:
    if is_secret_key(key):
        if isinstance(value, (list, tuple)):
            # Secret file references are safe to persist; secret values are not.
            return [redact(item) for item in value]
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, tuple):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        if value.lower().startswith(("bearer ", "basic ")):
            return REDACTED
        return value[:20000]
    return value


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_allowed_path(path_value: str, roots: tuple[Path, ...], *, must_exist: bool = True) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not any(is_relative_to(path, root.resolve()) for root in roots):
        raise ValueError(f"path is outside allowed roots: {path}")
    if must_exist and not path.exists():
        raise ValueError(f"path does not exist: {path}")
    return path


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(sha256_text(raw), expected_hash)