from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[2]
DEMO_DIR = REPO_ROOT / "demo"
WORKSPACE_DIR = Path(os.environ.get("AI_SERVING_OPS_WORKSPACE", BASE_DIR / "workspace")).resolve()
BACKUP_DIR = WORKSPACE_DIR / "backups"
DEFAULT_LOG_PATH = DEMO_DIR / "vllm_503_gpu_memory.log"
DEFAULT_CONFIG_PATH = DEMO_DIR / "vllm_config.json"


FRAMEWORK_PATTERNS = {
    "vllm": re.compile(r"\bvllm\b|gpu_memory_utilization|max_model_len", re.IGNORECASE),
    "ollama": re.compile(r"\bollama\b|/api/generate", re.IGNORECASE),
    "triton": re.compile(r"\btriton\b|model repository", re.IGNORECASE),
    "mindie": re.compile(r"\bmindie\b|ascend|cann|acl", re.IGNORECASE),
    "fastapi": re.compile(r"\bfastapi\b|uvicorn", re.IGNORECASE),
}


LOG_PATTERNS: dict[str, dict[str, Any]] = {
    "http_503": {
        "regex": re.compile(r"\b503\b|service unavailable|worker is not ready", re.IGNORECASE),
        "severity": "warning",
        "meaning": "Service endpoint is visible but model worker/backend is not ready.",
    },
    "model_load_timeout": {
        "regex": re.compile(r"model.*load.*timeout|load.*model.*timeout|worker state=initializing", re.IGNORECASE),
        "severity": "critical",
        "meaning": "Model loading exceeded startup window.",
    },
    "gpu_memory_pressure": {
        "regex": re.compile(r"cuda out of memory|oom|gpu.*memory|显存|内存", re.IGNORECASE),
        "severity": "critical",
        "meaning": "Accelerator memory pressure during model load or serving.",
    },
    "model_path_error": {
        "regex": re.compile(r"model path.*not found|no such file|cannot find model|模型.*不存在", re.IGNORECASE),
        "severity": "critical",
        "meaning": "Model path is missing or not mounted.",
    },
    "port_conflict": {
        "regex": re.compile(r"address already in use|port .* already|端口.*占用", re.IGNORECASE),
        "severity": "warning",
        "meaning": "Service port is already occupied.",
    },
}


def as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_path(value: str | None, default_path: Path) -> Path:
    if not value:
        return default_path.resolve()
    path = Path(value)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, REPO_ROOT / path, BASE_DIR / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return candidates[0].resolve()


def resolve_safe_read_path(value: str | None, default_path: Path) -> tuple[Path | None, str | None]:
    path = resolve_path(value, default_path)
    allowed_roots = [DEMO_DIR.resolve(), WORKSPACE_DIR.resolve()]
    if any(is_relative_to(path, root) for root in allowed_roots):
        return path, None
    return None, f"path is outside allowed roots: {path}. Set AI_SERVING_OPS_WORKSPACE for real configs."


def resolve_safe_write_path(value: str | None, default_path: Path) -> tuple[Path | None, str | None]:
    path, error = resolve_safe_read_path(value, default_path)
    if error or path is None:
        return None, error
    allow_demo_write = os.environ.get("AI_SERVING_OPS_ALLOW_DEMO_WRITE", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    if is_relative_to(path, WORKSPACE_DIR) or allow_demo_write:
        return path, None
    return None, f"refuse to modify demo/read-only path: {path}. Copy it under {WORKSPACE_DIR} for writable repairs."


def load_json_config(config_path: str | None = None) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    path, error = resolve_safe_read_path(config_path, DEFAULT_CONFIG_PATH)
    if error or path is None:
        return None, None, error
    if not path.exists():
        return None, path, "config file not found"
    try:
        data = json.loads(read_text_file(path))
    except json.JSONDecodeError as exc:
        return None, path, f"invalid JSON config: {exc}"
    if not isinstance(data, dict):
        return None, path, "config root must be a JSON object"
    return data, path, None


def inspect_service_health(url: str, timeout_seconds: float = 3.0) -> dict[str, Any]:
    if not url.strip():
        return {"ok": False, "error": "url is required"}
    started = time.perf_counter()
    request = Request(url, headers={"User-Agent": "ai-serving-ops/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(2048).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 400,
                "url": url,
                "status": response.status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "body_excerpt": body,
            }
    except HTTPError as exc:
        body = exc.read(2048).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "url": url,
            "status": exc.code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "body_excerpt": body,
            "suggestions": endpoint_suggestions(exc.code),
        }
    except URLError as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc.reason),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "suggestions": ["Connection failed. Check process, port mapping, firewall, and container health."],
        }


def endpoint_suggestions(status_code: int) -> list[str]:
    if status_code == 503:
        return ["503 often means the HTTP server is up but model workers are still loading or failed."]
    if status_code >= 500:
        return ["Server-side error. Inspect serving logs and accelerator memory."]
    return ["Unexpected HTTP status. Verify endpoint path and serving framework."]


def detect_serving_framework(url: str = "", log_path: str = "") -> dict[str, Any]:
    text = url
    path = resolve_path(log_path or None, DEFAULT_LOG_PATH)
    if path.exists():
        text += "\n" + read_text_file(path)[:5000]
    matches = [name for name, pattern in FRAMEWORK_PATTERNS.items() if pattern.search(text)]
    framework = matches[0] if matches else "generic"
    return {
        "ok": True,
        "framework": framework,
        "candidates": matches or ["generic"],
        "source": str(path) if path.exists() else "url only",
    }


def validate_model_path(model_path: str) -> dict[str, Any]:
    path = resolve_path(model_path, DEMO_DIR / "models")
    exists = path.exists()
    files = []
    if path.is_dir():
        files = [item.name for item in list(path.iterdir())[:20]]
    return {
        "ok": exists,
        "source": str(path),
        "exists": exists,
        "is_dir": path.is_dir(),
        "files": files,
        "suggestions": [] if exists else ["Model path is missing. Check Docker volume mount or config model_path."],
    }


def parse_serving_log(log_path: str = "", framework: str = "generic") -> dict[str, Any]:
    path = resolve_path(log_path or None, DEFAULT_LOG_PATH)
    if not path.exists():
        return {"ok": False, "source": str(path), "error": "log file not found"}
    text = read_text_file(path)
    matches: dict[str, list[dict[str, Any]]] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, info in LOG_PATTERNS.items():
            if info["regex"].search(line):
                matches.setdefault(name, []).append(
                    {
                        "line": line_no,
                        "text": line.strip()[:500],
                        "severity": info["severity"],
                        "meaning": info["meaning"],
                    }
                )
    return {
        "ok": True,
        "framework": framework,
        "source": str(path),
        "total_lines": len(text.splitlines()),
        "matched_patterns": sorted(matches),
        "evidence": {name: items[:5] for name, items in matches.items()},
    }


def verify_serving_config(config_path: str = "", framework: str = "generic") -> dict[str, Any]:
    config, path, error = load_json_config(config_path or None)
    if error or config is None or path is None:
        return {"ok": False, "source": str(path) if path else config_path or str(DEFAULT_CONFIG_PATH), "error": error}

    warnings: list[str] = []
    model_path = str(config.get("model_path", "")).strip()
    if not model_path:
        warnings.append("model_path is missing.")
    elif not validate_model_path(model_path).get("exists"):
        warnings.append(f"model_path does not exist or is not mounted: {model_path}")

    gpu_memory_utilization = config.get("gpu_memory_utilization")
    if isinstance(gpu_memory_utilization, (int, float)) and gpu_memory_utilization > 0.9:
        warnings.append("gpu_memory_utilization is high and may leave too little memory for runtime overhead.")

    max_model_len = config.get("max_model_len")
    if isinstance(max_model_len, int) and max_model_len > 16384:
        warnings.append("max_model_len is high and may increase KV cache memory pressure.")

    max_batch_size = config.get("max_batch_size")
    if isinstance(max_batch_size, int) and max_batch_size > 8:
        warnings.append("max_batch_size is high for memory-pressure scenarios.")

    return {
        "ok": not warnings,
        "framework": framework,
        "source": str(path),
        "warnings": warnings,
        "tracked_keys": {
            key: config.get(key)
            for key in [
                "service_name",
                "framework",
                "model_path",
                "port",
                "max_model_len",
                "gpu_memory_utilization",
                "tensor_parallel_size",
                "max_batch_size",
                "max_prefill_tokens",
                "startup_timeout_seconds",
            ]
            if key in config
        },
    }


def suggest_serving_config_patch(config_path: str = "", symptom: str = "", framework: str = "generic") -> dict[str, Any]:
    config, path, error = load_json_config(config_path or None)
    if error or config is None or path is None:
        return {"ok": False, "source": str(path) if path else config_path or str(DEFAULT_CONFIG_PATH), "error": error}

    patch: dict[str, Any] = {}
    reasons: list[str] = []
    symptom_lower = symptom.lower()
    memory_pressure = any(token in symptom_lower for token in ["oom", "out of memory", "gpu", "hbm", "memory", "503", "timeout", "显存", "内存"])

    gpu_memory_utilization = config.get("gpu_memory_utilization")
    if memory_pressure and isinstance(gpu_memory_utilization, (int, float)) and gpu_memory_utilization > 0.85:
        patch["gpu_memory_utilization"] = 0.85
        reasons.append("lower gpu_memory_utilization to leave runtime headroom during model load.")

    max_model_len = config.get("max_model_len")
    if memory_pressure and isinstance(max_model_len, int) and max_model_len > 8192:
        patch["max_model_len"] = 8192
        reasons.append("lower max_model_len to reduce KV cache memory pressure.")

    startup_timeout = config.get("startup_timeout_seconds")
    if isinstance(startup_timeout, int) and startup_timeout < 300:
        patch["startup_timeout_seconds"] = 300
        reasons.append("increase startup_timeout_seconds so health checks wait for large model loading.")

    max_batch_size = config.get("max_batch_size")
    if memory_pressure and isinstance(max_batch_size, int) and max_batch_size > 1:
        patch["max_batch_size"] = max(1, max_batch_size // 2)
        reasons.append("lower max_batch_size to reduce memory pressure.")

    return {
        "ok": True,
        "framework": framework,
        "source": str(path),
        "patch": patch,
        "reasons": reasons or ["No safe patch inferred from supported keys."],
        "dry_run_apply_arguments": {
            "config_path": str(path),
            "patch_json": json.dumps(patch, ensure_ascii=False),
            "dry_run": True,
        },
        "approval_required_for_write": True,
    }


def backup_config(config_path: str) -> dict[str, Any]:
    path, error = resolve_safe_read_path(config_path, DEFAULT_CONFIG_PATH)
    if error or path is None:
        return {"ok": False, "source": config_path or str(DEFAULT_CONFIG_PATH), "error": error}
    if not path.exists():
        return {"ok": False, "source": str(path), "error": "config file not found"}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"{path.stem}.{timestamp}{path.suffix}.bak"
    shutil.copy2(path, backup_path)
    return {"ok": True, "source": str(path), "backup_path": str(backup_path)}


def apply_serving_config_patch(config_path: str, patch_json: str = "", dry_run: bool = True) -> dict[str, Any]:
    config, read_path, error = load_json_config(config_path or None)
    if error or config is None or read_path is None:
        return {"ok": False, "source": str(read_path) if read_path else config_path or str(DEFAULT_CONFIG_PATH), "error": error}
    try:
        patch = json.loads(patch_json) if patch_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {"ok": False, "source": str(read_path), "error": f"invalid patch_json: {exc}"}
    if not isinstance(patch, dict):
        return {"ok": False, "source": str(read_path), "error": "patch_json must be a JSON object"}

    allowed_keys = {
        "gpu_memory_utilization",
        "max_model_len",
        "startup_timeout_seconds",
        "max_batch_size",
        "max_prefill_tokens",
        "health_check_timeout_ms",
    }
    rejected = sorted(set(patch) - allowed_keys)
    if rejected:
        return {"ok": False, "source": str(read_path), "error": f"unsupported patch keys: {rejected}", "allowed_keys": sorted(allowed_keys)}

    updated = dict(config)
    changes: dict[str, dict[str, Any]] = {}
    for key, value in patch.items():
        old_value = updated.get(key)
        if old_value != value:
            updated[key] = value
            changes[key] = {"old": old_value, "new": value}

    if dry_run:
        return {"ok": True, "source": str(read_path), "dry_run": True, "changes": changes, "updated_preview": updated}

    write_path, write_error = resolve_safe_write_path(config_path, DEFAULT_CONFIG_PATH)
    if write_error or write_path is None:
        return {"ok": False, "source": str(read_path), "error": write_error}
    backup_result = backup_config(str(write_path))
    if not backup_result.get("ok"):
        return {"ok": False, "source": str(write_path), "error": f"backup failed: {backup_result.get('error')}"}
    write_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "source": str(write_path), "dry_run": False, "backup_path": backup_result["backup_path"], "changes": changes}


def resolve_backup_path(backup_path: str) -> tuple[Path | None, str | None]:
    if not backup_path.strip():
        return None, "backup_path is required"
    path = Path(backup_path)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, BACKUP_DIR / path.name]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            if is_relative_to(resolved, BACKUP_DIR):
                return resolved, None
            return None, f"backup path outside backup dir: {resolved}"
    return None, f"backup not found under {BACKUP_DIR}"


def rollback_config(config_path: str, backup_path: str, dry_run: bool = True) -> dict[str, Any]:
    target, target_error = resolve_safe_read_path(config_path, DEFAULT_CONFIG_PATH)
    if target_error or target is None:
        return {"ok": False, "source": config_path, "error": target_error}
    backup, backup_error = resolve_backup_path(backup_path)
    if backup_error or backup is None:
        return {"ok": False, "source": str(target), "backup_path": backup_path, "error": backup_error}
    if dry_run:
        return {"ok": True, "source": str(target), "backup_path": str(backup), "dry_run": True, "would_replace": str(target)}
    write_path, write_error = resolve_safe_write_path(config_path, DEFAULT_CONFIG_PATH)
    if write_error or write_path is None:
        return {"ok": False, "source": str(target), "error": write_error}
    before = backup_config(str(write_path))
    if not before.get("ok"):
        return {"ok": False, "source": str(write_path), "error": f"backup before rollback failed: {before.get('error')}"}
    shutil.copy2(backup, write_path)
    return {"ok": True, "source": str(write_path), "backup_path": str(backup), "dry_run": False, "pre_rollback_backup_path": before["backup_path"]}


def verify_service_recovery(url: str, expected_status: int = 200, timeout_seconds: float = 3.0) -> dict[str, Any]:
    result = inspect_service_health(url, timeout_seconds)
    status = result.get("status")
    recovered = isinstance(status, int) and status == expected_status
    result.update({"expected_status": expected_status, "recovered": recovered, "verification": "passed" if recovered else "failed"})
    return result
