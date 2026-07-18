from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiops_agent.config import Settings
from aiops_agent.models import InferenceServiceManifest
from aiops_agent.security import REDACTED, redact, validate_manifest_policy, validate_probe_url


def test_manifest_is_strict_and_validates_parallelism(manifest_text: str) -> None:
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    assert manifest.metadata.name == "qwen-test"
    assert manifest.spec.vllm.tensor_parallel_size == 1
    with pytest.raises(ValidationError):
        InferenceServiceManifest.model_validate(
            {
                **manifest.model_dump(by_alias=True),
                "unknown": True,
            }
        )


def test_live_policy_requires_digest_and_allowed_path(settings: Settings, manifest_text: str) -> None:
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    live = replace(settings, profile="live")
    errors = validate_manifest_policy(manifest, live)
    assert "pinned by sha256" in " ".join(errors)
    manifest.spec.model.host_path = str(Path(settings.state_dir).parent / "outside")
    errors = validate_manifest_policy(manifest, settings)
    assert "outside allowed roots" in " ".join(errors)


def test_redaction_covers_nested_secrets_without_losing_file_references() -> None:
    value = {
        "API_TOKEN": "secret-value",
        "nested": {"password": "hidden", "safe": "visible"},
        "secrets": [{"name": "HF_TOKEN", "file": "/run/secret"}],
        "maxNumBatchedTokens": 512,
    }
    redacted = redact(value)
    assert redacted["API_TOKEN"] == REDACTED
    assert redacted["nested"]["password"] == REDACTED
    assert redacted["nested"]["safe"] == "visible"
    assert redacted["secrets"][0]["file"] == "/run/secret"
    assert redacted["maxNumBatchedTokens"] == 512


def test_probe_guard_rejects_external_hosts(settings: Settings) -> None:
    assert validate_probe_url("http://127.0.0.1:8000/v1/models", settings)
    with pytest.raises(ValueError, match="outside allowed CIDRs"):
        validate_probe_url("http://8.8.8.8:80/", settings)
