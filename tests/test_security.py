"""Security redaction and secret-key detection tests.

Restored from the pre-rename `test_models_security.py` (the manifest-policy and
probe-URL tests covered the removed vLLM subsystem and were not carried over).
"""

from __future__ import annotations

from termops.security import REDACTED, is_secret_key, redact


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


def test_secret_key_detection_covers_common_shapes() -> None:
    assert is_secret_key("api_key")
    assert is_secret_key("API_KEY")
    assert is_secret_key("Authorization")
    assert is_secret_key("githubToken")
    assert not is_secret_key("model")
    assert not is_secret_key("max_num_batched_tokens")


def test_bearer_strings_are_redacted() -> None:
    assert redact("Bearer abc123") == REDACTED
    assert redact("basic xyz") == REDACTED
    assert redact("plain output text") == "plain output text"
