from __future__ import annotations

import json
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
LEGACY_MCP_ROOT = Path(__file__).resolve().parents[1]
ASCEND_OPS_DIR = LEGACY_MCP_ROOT / "mcp-server-ascend-ops"
sys.path.insert(0, str(ASCEND_OPS_DIR))

from diagnostics import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_NPU_SMI_PATH,
    apply_config_patch,
    apply_mindie_config_patch,
    check_npu_status,
    diagnose_inference_issue,
    remediation_plan_markdown,
    suggest_config_patch,
    suggest_mindie_config_patch,
)


def load_module(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def main() -> int:
    # Legacy fixtures are available only under the explicit demo profile.
    os.environ["AIOPS_PROFILE"] = "demo"
    cases = load_cases(Path(__file__).with_name("ascend_cases.jsonl"))
    failures: list[str] = []
    ai_serving = load_module(
        "ai_serving_diagnostics", LEGACY_MCP_ROOT / "mcp-server-ai-serving-ops" / "diagnostics.py"
    )
    accelerator = load_module(
        "accelerator_diagnostics", LEGACY_MCP_ROOT / "mcp-server-accelerator-ops" / "diagnostics.py"
    )
    container_ops = load_module(
        "container_diagnostics", LEGACY_MCP_ROOT / "mcp-server-container-ops" / "diagnostics.py"
    )

    for case in cases:
        log_path = str(LEGACY_MCP_ROOT / case["log_path"])
        diagnosis = diagnose_inference_issue(case["symptom"], log_path)
        plan = remediation_plan_markdown(case["symptom"], log_path)
        matched_patterns = set(diagnosis["evidence"]["log"].get("matched_patterns", []))

        missing_patterns = sorted(set(case["expected_patterns"]) - matched_patterns)
        missing_keywords = [keyword for keyword in case["expected_keywords"] if keyword not in plan]

        if missing_patterns or missing_keywords:
            failures.append(
                json.dumps(
                    {
                        "id": case["id"],
                        "missing_patterns": missing_patterns,
                        "missing_keywords": missing_keywords,
                    },
                    ensure_ascii=False,
                )
            )

    patch_suggestion = suggest_mindie_config_patch(str(DEFAULT_CONFIG_PATH), "MindIE 503 HBM model load timeout")
    expected_patch_keys = {"max_batch_size", "max_prefill_tokens", "model_load_timeout_ms", "health_check_timeout_ms"}
    patch_keys = set(patch_suggestion.get("patch", {}))
    if not expected_patch_keys.issubset(patch_keys):
        failures.append(
            json.dumps(
                {
                    "id": "safe_repair_patch",
                    "missing_patch_keys": sorted(expected_patch_keys - patch_keys),
                },
                ensure_ascii=False,
            )
        )

    dry_run = apply_mindie_config_patch(
        str(DEFAULT_CONFIG_PATH),
        json.dumps(patch_suggestion.get("patch", {}), ensure_ascii=False),
        dry_run=True,
    )
    if not dry_run.get("ok") or not dry_run.get("dry_run") or not dry_run.get("changes"):
        failures.append(
            json.dumps(
                {
                    "id": "safe_repair_dry_run",
                    "result": dry_run,
                },
                ensure_ascii=False,
            )
        )

    generic_suggestion = suggest_config_patch(str(DEFAULT_CONFIG_PATH), "MindIE 503 HBM model load timeout")
    generic_dry_run = apply_config_patch(
        str(DEFAULT_CONFIG_PATH),
        json.dumps(generic_suggestion.get("patch", {}), ensure_ascii=False),
        dry_run=True,
    )
    if not generic_dry_run.get("ok") or not generic_dry_run.get("dry_run"):
        failures.append(
            json.dumps(
                {
                    "id": "generic_safe_repair_dry_run",
                    "result": generic_dry_run,
                },
                ensure_ascii=False,
            )
        )

    npu_summary = check_npu_status(use_mock_when_unavailable=True).get("summary", {})
    devices = npu_summary.get("devices", [])
    processes = npu_summary.get("processes", [])
    if not devices or devices[0].get("device_id") != 0 or not processes:
        failures.append(
            json.dumps(
                {
                    "id": "npu_structured_summary",
                    "fixture": str(DEFAULT_NPU_SMI_PATH),
                    "summary": npu_summary,
                },
                ensure_ascii=False,
            )
        )

    vllm_log = str(REPO_ROOT / "demo" / "vllm_503_gpu_memory.log")
    serving_log = ai_serving.parse_serving_log(vllm_log, "vllm")
    expected_serving_patterns = {"http_503", "model_load_timeout", "gpu_memory_pressure"}
    missing_serving_patterns = sorted(expected_serving_patterns - set(serving_log.get("matched_patterns", [])))
    if missing_serving_patterns:
        failures.append(
            json.dumps(
                {
                    "id": "vllm_503_serving_log",
                    "missing_patterns": missing_serving_patterns,
                    "result": serving_log,
                },
                ensure_ascii=False,
            )
        )

    vllm_config = str(REPO_ROOT / "demo" / "vllm_config.json")
    serving_suggestion = ai_serving.suggest_serving_config_patch(
        vllm_config,
        "vLLM 503 CUDA out of memory model load timeout",
        "vllm",
    )
    expected_serving_patch = {"gpu_memory_utilization", "max_model_len", "startup_timeout_seconds"}
    serving_patch_keys = set(serving_suggestion.get("patch", {}))
    if not expected_serving_patch.issubset(serving_patch_keys):
        failures.append(
            json.dumps(
                {
                    "id": "vllm_safe_repair_patch",
                    "missing_patch_keys": sorted(expected_serving_patch - serving_patch_keys),
                    "result": serving_suggestion,
                },
                ensure_ascii=False,
            )
        )

    serving_dry_run = ai_serving.apply_serving_config_patch(
        vllm_config,
        json.dumps(serving_suggestion.get("patch", {}), ensure_ascii=False),
        dry_run=True,
    )
    if not serving_dry_run.get("ok") or not serving_dry_run.get("dry_run") or not serving_dry_run.get("changes"):
        failures.append(
            json.dumps(
                {
                    "id": "vllm_safe_repair_dry_run",
                    "result": serving_dry_run,
                },
                ensure_ascii=False,
            )
        )

    old_force_mock = os.environ.get("ACCELERATOR_OPS_FORCE_MOCK")
    os.environ["ACCELERATOR_OPS_FORCE_MOCK"] = "true"
    pressure = accelerator.detect_memory_pressure("nvidia")
    if old_force_mock is None:
        os.environ.pop("ACCELERATOR_OPS_FORCE_MOCK", None)
    else:
        os.environ["ACCELERATOR_OPS_FORCE_MOCK"] = old_force_mock
    if not pressure.get("memory_pressure") or not pressure.get("processes"):
        failures.append(
            json.dumps(
                {
                    "id": "accelerator_memory_pressure",
                    "result": pressure,
                },
                ensure_ascii=False,
            )
        )

    container_health = container_ops.check_container_health("vllm-qwen")
    container_logs = container_ops.get_container_logs("vllm-qwen", 20)
    if container_health.get("ok") or "CUDA out of memory" not in container_logs.get("logs", ""):
        failures.append(
            json.dumps(
                {
                    "id": "container_fixture",
                    "health": container_health,
                    "logs": container_logs,
                },
                ensure_ascii=False,
            )
        )

    print(f"cases={len(cases)} failures={len(failures)}")
    for failure in failures:
        print(failure)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
