from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "fixtures"
DEFAULT_LOG_PATH = FIXTURES_DIR / "mindie_503_model_load_timeout.log"
DEFAULT_NPU_SMI_PATH = FIXTURES_DIR / "npu-smi-info.txt"
DEFAULT_CONFIG_PATH = FIXTURES_DIR / "mindie_config.json"
WORKSPACE_DIR = Path(os.environ.get("ASCEND_OPS_WORKSPACE", BASE_DIR / "workspace")).resolve()
BACKUP_DIR = WORKSPACE_DIR / "backups"


PATTERNS: dict[str, dict[str, Any]] = {
    "http_503": {
        "regex": re.compile(r"\b503\b|service unavailable", re.IGNORECASE),
        "severity": "warning",
        "meaning": "推理服务健康检查失败或后端尚未就绪。",
    },
    "model_load_timeout": {
        "regex": re.compile(r"model.*load.*timeout|load.*model.*timeout|模型.*加载.*超时", re.IGNORECASE),
        "severity": "critical",
        "meaning": "模型加载超过服务等待窗口，常见于权重路径错误、HBM 不足或初始化慢。",
    },
    "npu_memory_pressure": {
        "regex": re.compile(r"hbm|memory allocation|acl_error_rt_memory|内存|显存", re.IGNORECASE),
        "severity": "critical",
        "meaning": "NPU HBM 或运行时内存相关异常。",
    },
    "cann_env_error": {
        "regex": re.compile(r"ascend_home_path|cann|opp path|acl init|aclrtsetdevice", re.IGNORECASE),
        "severity": "critical",
        "meaning": "CANN/ACL 环境或设备初始化异常。",
    },
    "port_conflict": {
        "regex": re.compile(r"address already in use|port .* already|端口.*占用", re.IGNORECASE),
        "severity": "warning",
        "meaning": "推理服务监听端口被占用。",
    },
    "permission_error": {
        "regex": re.compile(r"permission denied|access denied|权限", re.IGNORECASE),
        "severity": "warning",
        "meaning": "模型、日志或配置文件权限不足。",
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


def resolve_existing_path(value: str | None, default_path: Path) -> Path:
    if not value:
        return default_path.resolve()

    path = Path(value)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, BASE_DIR / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return candidates[0].resolve()


def resolve_safe_read_path(value: str | None, default_path: Path) -> tuple[Path | None, str | None]:
    path = resolve_existing_path(value, default_path)
    allowed_roots = [FIXTURES_DIR.resolve(), WORKSPACE_DIR.resolve()]
    if any(is_relative_to(path, root) for root in allowed_roots):
        return path, None
    return None, (
        f"path is outside allowed roots: {path}. "
        f"Set ASCEND_OPS_WORKSPACE to the parent directory for real config files."
    )


def resolve_safe_write_path(value: str | None, default_path: Path) -> tuple[Path | None, str | None]:
    path, error = resolve_safe_read_path(value, default_path)
    if error or path is None:
        return None, error

    allow_fixture_write = os.environ.get("ASCEND_OPS_ALLOW_FIXTURE_WRITE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    if is_relative_to(path, WORKSPACE_DIR) or allow_fixture_write:
        return path, None
    return None, (
        f"refuse to modify fixture/read-only path: {path}. "
        f"Copy it under {WORKSPACE_DIR} or set ASCEND_OPS_WORKSPACE to a writable config root."
    )


def resolve_log_path(log_path: str | None) -> Path:
    if not log_path:
        return DEFAULT_LOG_PATH
    path = Path(log_path)
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


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


def parse_mindie_log_file(log_path: str | None = None) -> dict[str, Any]:
    path = resolve_log_path(log_path)
    if not path.exists():
        return {
            "ok": False,
            "source": str(path),
            "error": "log file not found",
            "hint": f"Try the bundled demo log: {DEFAULT_LOG_PATH}",
        }

    text = read_text_file(path)
    matches: dict[str, list[dict[str, Any]]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern_info in PATTERNS.items():
            if pattern_info["regex"].search(line):
                matches.setdefault(pattern_name, []).append(
                    {
                        "line": line_number,
                        "text": line.strip()[:500],
                        "severity": pattern_info["severity"],
                        "meaning": pattern_info["meaning"],
                    }
                )

    return {
        "ok": True,
        "source": str(path),
        "total_lines": len(text.splitlines()),
        "matched_patterns": sorted(matches),
        "evidence": {name: items[:5] for name, items in matches.items()},
    }


def check_cann_environment() -> dict[str, Any]:
    env_names = [
        "ASCEND_HOME_PATH",
        "ASCEND_OPP_PATH",
        "TOOLCHAIN_HOME",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PATH",
    ]
    env_snapshot = {name: os.environ.get(name, "") for name in env_names}
    missing = [name for name in ["ASCEND_HOME_PATH", "ASCEND_OPP_PATH"] if not env_snapshot.get(name)]
    path_warnings: list[str] = []

    ascend_home = env_snapshot.get("ASCEND_HOME_PATH")
    if ascend_home and not Path(ascend_home).exists():
        path_warnings.append(f"ASCEND_HOME_PATH does not exist: {ascend_home}")

    ascend_opp = env_snapshot.get("ASCEND_OPP_PATH")
    if ascend_opp and not Path(ascend_opp).exists():
        path_warnings.append(f"ASCEND_OPP_PATH does not exist: {ascend_opp}")

    return {
        "ok": not missing and not path_warnings,
        "missing_required_env": missing,
        "path_warnings": path_warnings,
        "npu_smi_found": shutil.which("npu-smi") is not None,
        "env_snapshot": env_snapshot,
        "suggestions": build_env_suggestions(missing, path_warnings),
    }


def build_env_suggestions(missing: list[str], path_warnings: list[str]) -> list[str]:
    suggestions: list[str] = []
    if missing:
        suggestions.append("确认已安装 CANN/Toolkit，并 source set_env.sh 或执行对应 Windows 环境初始化脚本。")
    if path_warnings:
        suggestions.append("环境变量存在但路径不可访问，优先检查 CANN 安装目录、容器挂载路径和用户权限。")
    if not missing and not path_warnings:
        suggestions.append("基础环境变量未发现明显问题，可继续检查日志、模型路径和 NPU 资源。")
    return suggestions


def check_npu_status(use_mock_when_unavailable: bool = True) -> dict[str, Any]:
    npu_smi = shutil.which("npu-smi")
    if npu_smi:
        started = time.perf_counter()
        try:
            process = subprocess.run(
                [npu_smi, "info"],
                capture_output=True,
                timeout=8,
                check=False,
            )
            output = process.stdout.decode("utf-8", errors="replace") or process.stderr.decode(
                "utf-8",
                errors="replace",
            )
            return {
                "ok": process.returncode == 0,
                "source": "npu-smi info",
                "returncode": process.returncode,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "summary": summarize_npu_smi(output),
                "raw_excerpt": output[:3000],
            }
        except Exception as exc:
            return {
                "ok": False,
                "source": "npu-smi info",
                "error": str(exc),
                "suggestions": ["npu-smi 存在但执行失败，检查驱动、容器设备挂载和用户权限。"],
            }

    if not use_mock_when_unavailable:
        return {
            "ok": False,
            "source": "npu-smi info",
            "error": "npu-smi not found",
            "suggestions": ["当前环境未发现 npu-smi；如在容器内运行，请检查 /dev/davinci* 设备挂载。"],
        }

    output = read_text_file(DEFAULT_NPU_SMI_PATH)
    return {
        "ok": True,
        "source": "mock fixture because npu-smi is unavailable",
        "summary": summarize_npu_smi(output),
        "raw_excerpt": output[:3000],
    }


def parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def parse_hbm_usage(value: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d+)\s*/\s*(\d+)", value)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def parse_percent(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", value)
    if match:
        return float(match.group(1))
    return None


def parse_npu_smi_output(output: str) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    processes: list[dict[str, Any]] = []
    in_process_section = False

    for raw_line in output.splitlines():
        line = raw_line.strip().strip("|").strip()
        if not line or line.startswith("+"):
            continue
        if "Process id" in line and "Memory" in line:
            in_process_section = True
            continue
        if line.startswith("NPU") and "Health" in line:
            in_process_section = False
            continue

        columns = [item.strip() for item in re.split(r"\s{2,}", line) if item.strip()]
        if not columns or parse_int(columns[0]) is None:
            continue

        if in_process_section:
            if len(columns) < 4:
                continue
            processes.append(
                {
                    "process_id": parse_int(columns[0]),
                    "device_id": parse_int(columns[1]),
                    "process_memory_mb": parse_int(columns[2]),
                    "process_name": columns[3],
                }
            )
            continue

        if len(columns) < 7:
            continue
        hbm_used, hbm_total = parse_hbm_usage(columns[5])
        hbm_percent = parse_percent(columns[6])
        if hbm_percent is None and hbm_used is not None and hbm_total:
            hbm_percent = round(hbm_used / hbm_total * 100, 2)
        devices.append(
            {
                "device_id": parse_int(columns[0]),
                "name": columns[1],
                "health": columns[2],
                "power_w": parse_int(columns[3]),
                "temperature_c": parse_int(columns[4]),
                "hbm_used_mb": hbm_used,
                "hbm_total_mb": hbm_total,
                "hbm_usage_percent": hbm_percent,
                "aicore_percent": parse_int(columns[7]) if len(columns) > 7 else None,
            }
        )

    return {
        "devices": devices,
        "processes": processes,
    }


def summarize_npu_smi(output: str) -> dict[str, Any]:
    lower = output.lower()
    structured = parse_npu_smi_output(output)
    warnings: list[str] = []
    for device in structured["devices"]:
        device_id = device.get("device_id")
        hbm_percent = device.get("hbm_usage_percent")
        health = str(device.get("health") or "").upper()
        temperature = device.get("temperature_c")
        if isinstance(hbm_percent, (int, float)) and hbm_percent >= 90:
            warnings.append(f"NPU {device_id} HBM usage is high: {hbm_percent}%.")
        if health and health not in {"OK", "NORMAL"}:
            warnings.append(f"NPU {device_id} health is {health}.")
        if isinstance(temperature, int) and temperature >= 80:
            warnings.append(f"NPU {device_id} temperature is high: {temperature}C.")
    if "error" in lower or "fault" in lower:
        warnings.append("Output contains error/fault markers.")
    if not output.strip():
        warnings.append("npu-smi returned empty output.")
    return {
        "warnings": warnings,
        "contains_hbm": "hbm" in lower,
        "contains_temperature": "temp" in lower or "temperature" in lower,
        "devices": structured["devices"],
        "processes": structured["processes"],
    }


def inspect_inference_endpoint(url: str, timeout_seconds: float = 3.0) -> dict[str, Any]:
    started = time.perf_counter()
    request = Request(url, headers={"User-Agent": "micro-mcp-ascend-ops/0.1"})
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
            "suggestions": ["连接失败，先检查服务进程、端口监听、防火墙和容器端口映射。"],
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def endpoint_suggestions(status_code: int) -> list[str]:
    if status_code == 503:
        return ["503 常见于模型仍在加载、后端 worker 未就绪或健康检查超时。建议结合 MindIE 日志确认。"]
    if status_code == 404:
        return ["404 表示接口路径可能不一致，检查服务路由或 OpenAI-compatible endpoint 配置。"]
    if status_code >= 500:
        return ["5xx 表示服务端异常，优先读取推理服务日志和模型加载日志。"]
    return ["HTTP 状态异常，结合服务日志继续定位。"]


def verify_mindie_config_file(config_path: str | None = None) -> dict[str, Any]:
    config, path, error = load_json_config(config_path)
    if error or config is None or path is None:
        return {
            "ok": False,
            "source": str(path) if path else config_path or str(DEFAULT_CONFIG_PATH),
            "error": error,
        }

    warnings: list[str] = []
    model_path = str(config.get("model_path", "")).strip()
    if not model_path:
        warnings.append("model_path is empty or missing.")

    max_batch_size = config.get("max_batch_size")
    if isinstance(max_batch_size, int) and max_batch_size > 8:
        warnings.append("max_batch_size is high for a memory-pressure scenario.")

    max_prefill_tokens = config.get("max_prefill_tokens")
    if isinstance(max_prefill_tokens, int) and max_prefill_tokens > 4096:
        warnings.append("max_prefill_tokens is high and may increase HBM pressure.")

    timeout_ms = config.get("model_load_timeout_ms")
    if isinstance(timeout_ms, int) and timeout_ms < 180000:
        warnings.append("model_load_timeout_ms may be too short for large model startup.")

    return {
        "ok": not warnings,
        "source": str(path),
        "warnings": warnings,
        "tracked_keys": {
            key: config.get(key)
            for key in [
                "model_path",
                "max_batch_size",
                "max_prefill_tokens",
                "model_load_timeout_ms",
                "health_check_timeout_ms",
            ]
            if key in config
        },
    }


def suggest_mindie_config_patch(config_path: str | None = None, symptom: str = "") -> dict[str, Any]:
    config, path, error = load_json_config(config_path)
    if error or config is None or path is None:
        return {
            "ok": False,
            "source": str(path) if path else config_path or str(DEFAULT_CONFIG_PATH),
            "error": error,
        }

    patch: dict[str, Any] = {}
    reasons: list[str] = []
    symptom_lower = symptom.lower()
    memory_pressure = any(token in symptom_lower for token in ["hbm", "memory", "显存", "内存", "503", "timeout"])

    max_batch_size = config.get("max_batch_size")
    if memory_pressure and isinstance(max_batch_size, int) and max_batch_size > 1:
        patch["max_batch_size"] = max(1, max_batch_size // 2)
        reasons.append("lower max_batch_size to reduce HBM pressure during model load and serving.")

    max_prefill_tokens = config.get("max_prefill_tokens")
    if memory_pressure and isinstance(max_prefill_tokens, int) and max_prefill_tokens > 2048:
        patch["max_prefill_tokens"] = max(1024, max_prefill_tokens // 2)
        reasons.append("lower max_prefill_tokens to reduce prefill memory spikes.")

    model_load_timeout_ms = config.get("model_load_timeout_ms")
    if isinstance(model_load_timeout_ms, int) and model_load_timeout_ms < 300000:
        patch["model_load_timeout_ms"] = 300000
        reasons.append("increase model_load_timeout_ms so health checks do not fail before large model initialization finishes.")

    health_check_timeout_ms = config.get("health_check_timeout_ms")
    if isinstance(health_check_timeout_ms, int) and health_check_timeout_ms < 10000:
        patch["health_check_timeout_ms"] = 10000
        reasons.append("increase health_check_timeout_ms for slow startup checks.")

    return {
        "ok": True,
        "source": str(path),
        "patch": patch,
        "reasons": reasons or ["No safe config patch was inferred from current keys."],
        "dry_run_apply_arguments": {
            "config_path": str(path),
            "patch_json": json.dumps(patch, ensure_ascii=False),
            "dry_run": True,
        },
        "approval_required_for_write": True,
    }


def backup_config_file(config_path: str | None = None) -> dict[str, Any]:
    path, error = resolve_safe_read_path(config_path, DEFAULT_CONFIG_PATH)
    if error or path is None:
        return {
            "ok": False,
            "source": config_path or str(DEFAULT_CONFIG_PATH),
            "error": error,
        }
    if not path.exists():
        return {
            "ok": False,
            "source": str(path),
            "error": "config file not found",
        }

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"{path.stem}.{timestamp}{path.suffix}.bak"
    shutil.copy2(path, backup_path)
    return {
        "ok": True,
        "source": str(path),
        "backup_path": str(backup_path),
        "note": "backup created before any config mutation.",
    }


def apply_mindie_config_patch(
    config_path: str | None = None,
    patch_json: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    config, read_path, error = load_json_config(config_path)
    if error or config is None or read_path is None:
        return {
            "ok": False,
            "source": str(read_path) if read_path else config_path or str(DEFAULT_CONFIG_PATH),
            "error": error,
        }

    try:
        patch = json.loads(patch_json) if patch_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "source": str(read_path),
            "error": f"invalid patch_json: {exc}",
        }
    if not isinstance(patch, dict):
        return {
            "ok": False,
            "source": str(read_path),
            "error": "patch_json must be a JSON object",
        }

    allowed_keys = {"max_batch_size", "max_prefill_tokens", "model_load_timeout_ms", "health_check_timeout_ms"}
    rejected_keys = sorted(set(patch) - allowed_keys)
    if rejected_keys:
        return {
            "ok": False,
            "source": str(read_path),
            "error": f"patch contains unsupported keys: {rejected_keys}",
            "allowed_keys": sorted(allowed_keys),
        }

    updated = dict(config)
    changes: dict[str, dict[str, Any]] = {}
    for key, new_value in patch.items():
        old_value = updated.get(key)
        if old_value != new_value:
            updated[key] = new_value
            changes[key] = {"old": old_value, "new": new_value}

    if dry_run:
        return {
            "ok": True,
            "source": str(read_path),
            "dry_run": True,
            "changes": changes,
            "updated_preview": updated,
            "note": "No file was changed. Re-run with dry_run=false after Hub approval to write.",
        }

    write_path, write_error = resolve_safe_write_path(config_path, DEFAULT_CONFIG_PATH)
    if write_error or write_path is None:
        return {
            "ok": False,
            "source": str(read_path),
            "error": write_error,
        }

    backup_result = backup_config_file(str(write_path))
    if not backup_result.get("ok"):
        return {
            "ok": False,
            "source": str(write_path),
            "error": f"backup failed: {backup_result.get('error')}",
        }

    write_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "source": str(write_path),
        "dry_run": False,
        "backup_path": backup_result["backup_path"],
        "changes": changes,
    }


def backup_config(config_path: str | None = None) -> dict[str, Any]:
    return backup_config_file(config_path)


def suggest_config_patch(config_path: str | None = None, symptom: str = "") -> dict[str, Any]:
    return suggest_mindie_config_patch(config_path, symptom)


def apply_config_patch(
    config_path: str | None = None,
    patch_json: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    return apply_mindie_config_patch(config_path, patch_json, dry_run)


def resolve_backup_path(backup_path: str) -> tuple[Path | None, str | None]:
    if not backup_path.strip():
        return None, "backup_path is required"
    path = Path(backup_path)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, BASE_DIR / path, BACKUP_DIR / path.name]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            if is_relative_to(resolved, BACKUP_DIR):
                return resolved, None
            return None, f"backup path is outside allowed backup dir: {resolved}"
    return None, f"backup file not found under allowed backup dir: {BACKUP_DIR}"


def rollback_config(
    config_path: str | None = None,
    backup_path: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    target_path, target_error = resolve_safe_read_path(config_path, DEFAULT_CONFIG_PATH)
    if target_error or target_path is None:
        return {
            "ok": False,
            "source": config_path or str(DEFAULT_CONFIG_PATH),
            "error": target_error,
        }

    backup, backup_error = resolve_backup_path(backup_path)
    if backup_error or backup is None:
        return {
            "ok": False,
            "source": str(target_path),
            "backup_path": backup_path,
            "error": backup_error,
        }

    backup_text = read_text_file(backup)
    if dry_run:
        return {
            "ok": True,
            "source": str(target_path),
            "backup_path": str(backup),
            "dry_run": True,
            "would_replace": str(target_path),
            "backup_excerpt": backup_text[:1200],
            "note": "No file was changed. Re-run with dry_run=false after Hub approval to restore backup.",
        }

    write_path, write_error = resolve_safe_write_path(config_path, DEFAULT_CONFIG_PATH)
    if write_error or write_path is None:
        return {
            "ok": False,
            "source": str(target_path),
            "error": write_error,
        }

    before_rollback_backup = backup_config_file(str(write_path))
    if not before_rollback_backup.get("ok"):
        return {
            "ok": False,
            "source": str(write_path),
            "error": f"backup before rollback failed: {before_rollback_backup.get('error')}",
        }

    shutil.copy2(backup, write_path)
    return {
        "ok": True,
        "source": str(write_path),
        "backup_path": str(backup),
        "dry_run": False,
        "pre_rollback_backup_path": before_rollback_backup["backup_path"],
        "note": "Config restored from backup.",
    }


def verify_service_recovery(
    url: str,
    expected_status: int = 200,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    if not url.strip():
        return {
            "ok": False,
            "error": "url is required",
            "suggestions": ["Provide a health endpoint, for example http://127.0.0.1:1025/health."],
        }
    result = inspect_inference_endpoint(url, timeout_seconds)
    status = result.get("status")
    recovered = isinstance(status, int) and status == expected_status
    result.update(
        {
            "expected_status": expected_status,
            "recovered": recovered,
            "verification": "passed" if recovered else "failed",
        }
    )
    return result


def restart_service_dry_run(service_name: str, restart_command: str = "") -> dict[str, Any]:
    command = restart_command.strip() or f"systemctl restart {service_name}"
    return {
        "ok": True,
        "service_name": service_name,
        "proposed_command": command,
        "risk_level": "high",
        "will_execute": False,
        "required_checks_before_execution": [
            "Confirm the service is not handling production traffic or has a failover instance.",
            "Confirm config backup exists if config was changed.",
            "Confirm rollback command or previous config is available.",
            "Execute through mcp-server-devops.run_shell_command only after Hub approval.",
        ],
    }


def restart_service_with_approval(
    service_name: str,
    restart_command: str = "",
    execute: bool = False,
    confirm_text: str = "",
) -> dict[str, Any]:
    plan = restart_service_dry_run(service_name, restart_command)
    command = plan["proposed_command"]
    if not execute:
        plan.update(
            {
                "executed": False,
                "approval_required": True,
                "note": "Dry-run only. Set execute=true with confirm_text equal to service_name after Hub approval.",
            }
        )
        return plan

    if confirm_text.strip() != service_name:
        return {
            "ok": False,
            "service_name": service_name,
            "proposed_command": command,
            "executed": False,
            "error": "confirm_text must exactly match service_name.",
        }

    allow_restart = os.environ.get("ASCEND_OPS_ALLOW_SERVICE_RESTART", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    allowed_services = {
        item.strip()
        for item in os.environ.get("ASCEND_OPS_ALLOWED_SERVICES", "mindie-llm").split(",")
        if item.strip()
    }
    if not allow_restart:
        return {
            "ok": False,
            "service_name": service_name,
            "proposed_command": command,
            "executed": False,
            "error": "Service restart execution is disabled. Set ASCEND_OPS_ALLOW_SERVICE_RESTART=true only in a controlled environment.",
        }
    if service_name not in allowed_services:
        return {
            "ok": False,
            "service_name": service_name,
            "proposed_command": command,
            "executed": False,
            "error": f"service_name is not in ASCEND_OPS_ALLOWED_SERVICES: {sorted(allowed_services)}",
        }

    started = time.perf_counter()
    process = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return {
        "ok": process.returncode == 0,
        "service_name": service_name,
        "proposed_command": command,
        "executed": True,
        "returncode": process.returncode,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "stdout": process.stdout.decode("utf-8", errors="replace")[:2000],
        "stderr": process.stderr.decode("utf-8", errors="replace")[:2000],
    }


def diagnose_inference_issue(symptom: str, log_path: str | None = None) -> dict[str, Any]:
    log_result = parse_mindie_log_file(log_path)
    env_result = check_cann_environment()
    npu_result = check_npu_status(use_mock_when_unavailable=True)
    patterns = set(log_result.get("matched_patterns", []))
    symptom_lower = symptom.lower()

    if "model_load_timeout" in patterns and "npu_memory_pressure" in patterns:
        root_cause = "模型加载超时并伴随 HBM/内存异常，优先怀疑 NPU 显存不足或并发进程占用。"
        confidence = "high"
    elif "model_load_timeout" in patterns:
        root_cause = "服务返回异常前出现模型加载超时，推理后端大概率尚未就绪。"
        confidence = "high"
    elif "cann_env_error" in patterns or env_result["missing_required_env"]:
        root_cause = "CANN/ACL 环境或设备初始化异常，优先检查环境变量、驱动和容器设备挂载。"
        confidence = "medium"
    elif "port_conflict" in patterns:
        root_cause = "服务端口冲突，进程可能没有成功监听目标端口。"
        confidence = "medium"
    elif "503" in symptom_lower or "service unavailable" in symptom_lower:
        root_cause = "用户现象是 503，但日志证据不足；建议先确认服务健康检查和模型加载状态。"
        confidence = "low"
    else:
        root_cause = "当前证据不足，建议继续补充日志、服务健康检查和 NPU 状态。"
        confidence = "low"

    return {
        "symptom": symptom,
        "root_cause_hypothesis": root_cause,
        "confidence": confidence,
        "evidence": {
            "log": log_result,
            "environment": {
                "missing_required_env": env_result["missing_required_env"],
                "path_warnings": env_result["path_warnings"],
                "npu_smi_found": env_result["npu_smi_found"],
            },
            "npu": npu_result["summary"],
        },
        "next_actions": build_next_actions(patterns, env_result, npu_result),
    }


def build_next_actions(
    patterns: set[str],
    env_result: dict[str, Any],
    npu_result: dict[str, Any],
) -> dict[str, list[str]]:
    read_only = [
        "读取 MindIE/CANN 日志中首次 ERROR 前后 100 行。",
        "执行 npu-smi info，确认 NPU 健康状态、HBM 使用率和残留进程。",
        "检查推理服务端口监听和健康检查接口返回。",
    ]
    low_risk = [
        "修正本地日志路径、模型路径或健康检查 URL 后重新执行诊断。",
        "将本次故障现象和确认原因写入 memory，便于后续相似问题复用。",
    ]
    needs_approval: list[str] = []

    if "model_load_timeout" in patterns or "npu_memory_pressure" in patterns:
        needs_approval.append("如确认有无关残留推理进程占用 HBM，再审批执行停止进程或重启服务。")
        needs_approval.append("如需要降低 max_batch_size/max_prefill_tokens 等配置，先备份配置并审批修改。")
    if env_result["missing_required_env"] or env_result["path_warnings"]:
        low_risk.append("重新加载 CANN set_env 脚本后复查环境变量。")
    if npu_result.get("summary", {}).get("warnings"):
        needs_approval.append("如 NPU 状态异常，升级到人工确认硬件/驱动层面排查。")

    return {
        "read_only_checks": read_only,
        "low_risk_actions": low_risk,
        "requires_human_approval": needs_approval or ["暂未建议执行高风险动作。"],
    }


def remediation_plan_markdown(symptom: str, log_path: str | None = None) -> str:
    diagnosis = diagnose_inference_issue(symptom, log_path)
    actions = diagnosis["next_actions"]
    lines = [
        "# Ascend Inference Ops Plan",
        "",
        f"Symptom: {diagnosis['symptom']}",
        f"Root cause hypothesis: {diagnosis['root_cause_hypothesis']}",
        f"Confidence: {diagnosis['confidence']}",
        "",
        "## Evidence",
        f"- Log patterns: {', '.join(diagnosis['evidence']['log'].get('matched_patterns', [])) or 'none'}",
        f"- Missing env: {', '.join(diagnosis['evidence']['environment']['missing_required_env']) or 'none'}",
        f"- NPU warnings: {', '.join(diagnosis['evidence']['npu'].get('warnings', [])) or 'none'}",
        "",
        "## Read-only Checks",
    ]
    lines.extend(f"- {item}" for item in actions["read_only_checks"])
    lines.append("")
    lines.append("## Low-risk Actions")
    lines.extend(f"- {item}" for item in actions["low_risk_actions"])
    lines.append("")
    lines.append("## Requires Human Approval")
    lines.extend(f"- {item}" for item in actions["requires_human_approval"])
    return "\n".join(lines)
