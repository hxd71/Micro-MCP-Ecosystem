from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aiops_agent.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    model_dir = tmp_path / "models" / "qwen"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
    return replace(
        Settings.load(profile="test"),
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        run_dir=tmp_path / "run",
        allowed_model_roots=(tmp_path / "models",),
        allowed_secret_roots=(tmp_path / "secrets",),
        monitor_enabled=False,
        approval_ttl_seconds=900,
    )


@pytest.fixture
def manifest_text(settings: Settings) -> str:
    model_path = settings.allowed_model_roots[0] / "qwen"
    return f"""apiVersion: aiops.local/v1alpha1
kind: InferenceService
metadata:
  name: qwen-test
spec:
  image: vllm/vllm-openai:test
  model:
    hostPath: {model_path.as_posix()}
    containerPath: /model
  gpu:
    deviceIds: [\"0\"]
  endpoint:
    bindAddress: 127.0.0.1
    hostPort: 18000
    healthPath: /v1/models
  vllm:
    tensorParallelSize: 1
    maxModelLen: 8192
    gpuMemoryUtilization: 0.85
  monitoring:
    intervalSeconds: 60
    failureThreshold: 3
"""
