from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import InferenceServiceManifest, Severity

LOG_PATTERNS: dict[str, tuple[tuple[str, ...], Severity, str, str]] = {
    "GPU_OUT_OF_MEMORY": (
        ("cuda out of memory", "torch.cuda.outofmemoryerror", "out of memory"),
        Severity.CRITICAL,
        "vLLM worker reported accelerator memory exhaustion.",
        "Reduce context and concurrency before changing the GPU reservation ratio.",
    ),
    "VLLM_CACHE_BUDGET_EXHAUSTED": (
        ("no available memory for the cache blocks", "no available memory for cache blocks"),
        Severity.HIGH,
        "vLLM could not reserve even one KV cache block after loading the model.",
        "On an otherwise idle low-VRAM GPU, reserve enough memory for KV cache and bound context/concurrency.",
    ),
    "MODEL_LOAD_TIMEOUT": (
        ("model load timeout", "timed out while loading", "startup timeout"),
        Severity.HIGH,
        "Model loading did not complete within the configured startup window.",
        "Increase startupTimeoutSeconds only after confirming that model loading is making progress.",
    ),
    "SERVICE_HTTP_5XX": (
        (" 500 ", " 502 ", " 503 ", " 504 ", "service unavailable"),
        Severity.HIGH,
        "The serving process emitted an HTTP 5xx response.",
        "Inspect the linked container and log observations before proposing a restart.",
    ),
}


def log_evidence(logs: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(logs.splitlines(), start=1):
        lowered = line.lower()
        for code, (patterns, severity, meaning, remediation) in LOG_PATTERNS.items():
            if any(pattern in lowered for pattern in patterns):
                matches.append(
                    {
                        "code": code,
                        "severity": severity.value,
                        "meaning": meaning,
                        "remediation": remediation,
                        "line": line_number,
                        "text": line.strip()[:500],
                    }
                )
    return matches[:50]


def config_findings(manifest: InferenceServiceManifest) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    config = manifest.spec.vllm
    if config.gpu_memory_utilization > 0.95:
        findings.append(
            {
                "code": "VLLM_GPU_MEMORY_HEADROOM_LOW",
                "severity": Severity.MEDIUM,
                "confidence": 0.9,
                "title": "vLLM reservation leaves little explicit GPU headroom",
                "detail": f"gpuMemoryUtilization={config.gpu_memory_utilization} reserves over 95% of memory.",
                "remediation": "Correlate health, OOM and cache-block evidence before changing this value.",
            }
        )
    if config.max_model_len > 16384:
        findings.append(
            {
                "code": "VLLM_CONTEXT_WINDOW_PRESSURE",
                "severity": Severity.MEDIUM,
                "confidence": 0.8,
                "title": "Configured context window increases KV cache pressure",
                "detail": f"maxModelLen={config.max_model_len} can consume substantial GPU memory.",
                "remediation": "Reduce maxModelLen unless the workload requires the configured context window.",
            }
        )
    model_path = Path(manifest.spec.model.host_path)
    if not model_path.exists():
        findings.append(
            {
                "code": "MODEL_PATH_MISSING",
                "severity": Severity.CRITICAL,
                "confidence": 1.0,
                "title": "Model path is missing",
                "detail": f"The configured model path does not exist: {model_path}",
                "remediation": "Correct the managed model mount before deploying a new revision.",
            }
        )
    return findings


def tuned_memory_manifest(
    manifest: InferenceServiceManifest,
    finding_codes: set[str],
    gpu_status: dict[str, Any],
) -> InferenceServiceManifest:
    """Produce a bounded proposal from observed memory failures and device capacity."""
    tuned = manifest.model_copy(deep=True)
    config = tuned.spec.vllm
    selected = {int(item) for item in tuned.spec.gpu.device_ids}
    totals = [
        int(device.get("memory_total_mb", 0))
        for device in gpu_status.get("devices", [])
        if int(device.get("index", -1)) in selected and int(device.get("memory_total_mb", 0)) > 0
    ]
    smallest_gpu_mb = min(totals) if totals else 0

    if smallest_gpu_mb and smallest_gpu_mb <= 6144:
        max_len = min(config.max_model_len, 1024)
        max_seqs = 4
        config.enforce_eager = True
    elif smallest_gpu_mb and smallest_gpu_mb <= 12288:
        max_len = min(config.max_model_len, 4096)
        max_seqs = 16
    else:
        max_len = min(config.max_model_len, 8192)
        max_seqs = 64

    config.max_model_len = max_len
    config.max_num_seqs = min(config.max_num_seqs or max_seqs, max_seqs)
    config.max_num_batched_tokens = max_len

    if "VLLM_CACHE_BUDGET_EXHAUSTED" in finding_codes and config.gpu_memory_utilization < 0.9:
        config.gpu_memory_utilization = 0.9
    elif finding_codes & {"GPU_OUT_OF_MEMORY", "VLLM_GPU_MEMORY_HEADROOM_LOW"}:
        config.gpu_memory_utilization = min(config.gpu_memory_utilization, 0.9)
    return tuned


def security_findings(posture: dict[str, Any]) -> list[dict[str, Any]]:
    checks = posture.get("checks") or {}
    definitions = [
        ("privileged", True, "CONTAINER_PRIVILEGED", Severity.CRITICAL, "Container runs in privileged mode"),
        (
            "host_network",
            True,
            "CONTAINER_HOST_NETWORK",
            Severity.HIGH,
            "Container shares the host network namespace",
        ),
        (
            "host_pid",
            True,
            "CONTAINER_HOST_PID",
            Severity.CRITICAL,
            "Container shares the host PID namespace",
        ),
        (
            "read_only_rootfs",
            False,
            "ROOTFS_WRITABLE",
            Severity.MEDIUM,
            "Container root filesystem is writable",
        ),
        (
            "no_new_privileges",
            False,
            "NO_NEW_PRIVILEGES_MISSING",
            Severity.MEDIUM,
            "No-new-privileges is not enabled",
        ),
        (
            "mutable_image_tag",
            True,
            "IMAGE_NOT_PINNED",
            Severity.HIGH,
            "Container image is not pinned by digest",
        ),
    ]
    findings: list[dict[str, Any]] = []
    for key, bad_value, code, severity, title in definitions:
        if checks.get(key) == bad_value:
            findings.append(
                {
                    "code": code,
                    "severity": severity,
                    "confidence": 1.0,
                    "title": title,
                    "detail": f"Docker inspect reported {key}={checks.get(key)!r}.",
                    "remediation": "Deploy a policy-compliant managed service revision.",
                }
            )
    if checks.get("cap_add"):
        findings.append(
            {
                "code": "CAPABILITIES_ADDED",
                "severity": Severity.HIGH,
                "confidence": 1.0,
                "title": "Container adds Linux capabilities",
                "detail": f"Added capabilities: {checks['cap_add']}",
                "remediation": "Remove added capabilities and drop all capabilities by default.",
            }
        )
    if checks.get("sensitive_writable_mounts"):
        findings.append(
            {
                "code": "SENSITIVE_HOST_MOUNT",
                "severity": Severity.CRITICAL,
                "confidence": 1.0,
                "title": "Container has sensitive writable host mounts",
                "detail": f"Sensitive mounts: {checks['sensitive_writable_mounts']}",
                "remediation": "Remove the mount or make a narrowly scoped path read-only.",
            }
        )
    if checks.get("public_ports"):
        findings.append(
            {
                "code": "PUBLIC_PORT_EXPOSURE",
                "severity": Severity.MEDIUM,
                "confidence": 1.0,
                "title": "Inference endpoint is exposed on all interfaces",
                "detail": f"Public bindings: {checks['public_ports']}",
                "remediation": "Bind to loopback or an explicitly controlled interface.",
            }
        )
    if checks.get("user") in {"", "0", "root", "root/default"}:
        findings.append(
            {
                "code": "CONTAINER_ROOT_USER",
                "severity": Severity.MEDIUM,
                "confidence": 1.0,
                "title": "Container process runs as root",
                "detail": f"Docker inspect reported user={checks.get('user')!r}.",
                "remediation": "Use a verified vLLM image with a non-root runtime user when supported.",
            }
        )
    return findings
