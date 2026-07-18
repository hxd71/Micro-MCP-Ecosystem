from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiops_agent.diagnostics import log_evidence, tuned_memory_manifest
from aiops_agent.models import InferenceServiceManifest
from aiops_agent.providers import build_vllm_launch


def test_bounded_python_module_launch_contract(manifest_text: str) -> None:
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    config = manifest.spec.vllm
    config.launch_mode = "python-module"
    config.engine_version = "v0"
    config.max_model_len = 512
    config.gpu_memory_utilization = 0.9
    config.enforce_eager = True
    config.max_num_seqs = 4
    config.max_num_batched_tokens = 512
    config.swap_space_gib = 1
    config.disable_frontend_multiprocessing = True

    launch = build_vllm_launch(manifest)

    assert launch["entrypoint"] == ["python", "-m", "vllm.entrypoints.openai.api_server"]
    assert launch["environment"] == {
        "DO_NOT_TRACK": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "VLLM_USE_V1": "0",
    }
    command = launch["command"]
    assert command[command.index("--max-model-len") + 1] == "512"
    assert command[command.index("--gpu-memory-utilization") + 1] == "0.9"
    assert command[command.index("--max-num-seqs") + 1] == "4"
    assert "--enforce-eager" in command
    assert "--disable-frontend-multiprocessing" in command


def test_batch_budget_cannot_be_smaller_than_context(manifest_text: str) -> None:
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    raw = manifest.model_dump(by_alias=True)
    raw["spec"]["vllm"]["maxModelLen"] = 1024
    raw["spec"]["vllm"]["maxNumBatchedTokens"] = 512
    with pytest.raises(ValidationError, match="maxNumBatchedTokens"):
        InferenceServiceManifest.model_validate(raw)


def test_low_vram_cache_failure_increases_reservation_and_bounds_workload(
    manifest_text: str,
) -> None:
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    manifest.spec.vllm.gpu_memory_utilization = 0.7
    tuned = tuned_memory_manifest(
        manifest,
        {"VLLM_CACHE_BUDGET_EXHAUSTED"},
        {"devices": [{"index": 0, "memory_total_mb": 4096}]},
    )

    assert manifest.spec.vllm.gpu_memory_utilization == 0.7
    assert tuned.spec.vllm.gpu_memory_utilization == 0.9
    assert tuned.spec.vllm.max_model_len == 1024
    assert tuned.spec.vllm.max_num_seqs == 4
    assert tuned.spec.vllm.max_num_batched_tokens == 1024
    assert tuned.spec.vllm.enforce_eager is True


def test_cache_block_log_has_specific_evidence() -> None:
    matches = log_evidence("ValueError: No available memory for the cache blocks.")
    assert [item["code"] for item in matches] == ["VLLM_CACHE_BUDGET_EXHAUSTED"]
    assert "reserve enough memory" in matches[0]["remediation"]
