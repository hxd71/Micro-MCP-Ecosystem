from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .models import InferenceServiceManifest

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


def image_registry(image: str) -> str:
    first = image.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "docker.io"


def validate_manifest_policy(manifest: InferenceServiceManifest, settings: Settings) -> list[str]:
    errors: list[str] = []
    try:
        ensure_allowed_path(manifest.spec.model.host_path, settings.allowed_model_roots)
    except ValueError as exc:
        errors.append(str(exc))

    for secret_file in manifest.spec.secrets:
        try:
            secret_path = ensure_allowed_path(secret_file.file, settings.allowed_secret_roots)
            if os.name != "nt" and secret_path.stat().st_mode & 0o077:
                errors.append(f"secret file must not be group/world accessible: {secret_path}")
        except ValueError as exc:
            errors.append(str(exc))

    if settings.profile == "live" and "@sha256:" not in manifest.spec.image:
        errors.append("live profile requires an image pinned by sha256 digest")
    if settings.allowed_registries and image_registry(manifest.spec.image) not in settings.allowed_registries:
        errors.append(f"image registry is not allowed: {image_registry(manifest.spec.image)}")
    return errors


def validate_probe_url(url: str, settings: Settings) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("probe URL must be plain HTTP without credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("probe URL port is invalid")

    allowed_networks = [ipaddress.ip_network(value, strict=False) for value in settings.allowed_probe_cidrs]
    try:
        addresses = {ipaddress.ip_address(parsed.hostname)}
    except ValueError:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        }
    if not addresses or any(
        not any(address in network for network in allowed_networks) for address in addresses
    ):
        raise ValueError(f"probe target is outside allowed CIDRs: {parsed.hostname}")
    return url


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(sha256_text(raw), expected_hash)
