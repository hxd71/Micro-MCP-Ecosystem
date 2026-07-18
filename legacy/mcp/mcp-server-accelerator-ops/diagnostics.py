from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "fixtures"
REPO_ROOT = BASE_DIR.parent
NVIDIA_FIXTURE = FIXTURES_DIR / "nvidia-smi.txt"
ASCEND_FIXTURE = REPO_ROOT / "mcp-server-ascend-ops" / "fixtures" / "npu-smi-info.txt"


def as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def run_command(args: list[str], timeout: int = 8) -> tuple[int, str, str, float]:
    started = time.perf_counter()
    process = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")
    return process.returncode, stdout, stderr, round((time.perf_counter() - started) * 1000, 2)


def detect_provider(provider: str = "auto") -> str:
    normalized = provider.strip().lower() or "auto"
    if normalized in {"nvidia", "ascend"}:
        return normalized
    if shutil.which("nvidia-smi"):
        return "nvidia"
    if shutil.which("npu-smi"):
        return "ascend"
    return os.environ.get("ACCELERATOR_OPS_MOCK_PROVIDER", "nvidia").strip().lower() or "nvidia"


def parse_nvidia_smi(output: str) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    processes: list[dict[str, Any]] = []
    current_gpu: int | None = None
    for line in output.splitlines():
        stripped = line.strip().strip("|").strip()
        gpu_match = re.match(r"(\d+)\s+(.+?)\s{2,}.*", stripped)
        if gpu_match and "NVIDIA" in stripped and "MiB" not in stripped:
            current_gpu = int(gpu_match.group(1))
            devices.append({"device_id": current_gpu, "name": gpu_match.group(2).strip()})
            continue
        mem_match = re.search(r"(\d+)MiB\s*/\s*(\d+)MiB", stripped)
        temp_match = re.search(r"(\d+)C", stripped)
        util_match = re.search(r"\|\s*(\d+)%\s+Default", line)
        if mem_match and devices:
            used = int(mem_match.group(1))
            total = int(mem_match.group(2))
            devices[-1].update(
                {
                    "health": "OK",
                    "memory_used_mb": used,
                    "memory_total_mb": total,
                    "memory_usage_percent": round(used / total * 100, 2) if total else None,
                    "temperature_c": int(temp_match.group(1)) if temp_match else None,
                    "utilization_percent": int(util_match.group(1)) if util_match else None,
                }
            )
            continue
        process_match = re.match(r"(\d+)\s+\S+\s+\S+\s+(\d+)\s+\S+\s+(.+?)\s+(\d+)MiB", stripped)
        if process_match:
            processes.append(
                {
                    "device_id": int(process_match.group(1)),
                    "process_id": int(process_match.group(2)),
                    "process_name": process_match.group(3).strip(),
                    "process_memory_mb": int(process_match.group(4)),
                }
            )
    return {"devices": devices, "processes": processes}


def parse_ascend_npu_smi(output: str) -> dict[str, Any]:
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
        if in_process_section and len(columns) >= 4:
            processes.append(
                {
                    "device_id": parse_int(columns[1]),
                    "process_id": parse_int(columns[0]),
                    "process_memory_mb": parse_int(columns[2]),
                    "process_name": columns[3],
                }
            )
            continue
        if len(columns) >= 7:
            hbm = re.search(r"(\d+)\s*/\s*(\d+)", columns[5])
            percent = re.search(r"(\d+(?:\.\d+)?)%", columns[6])
            used = int(hbm.group(1)) if hbm else None
            total = int(hbm.group(2)) if hbm else None
            devices.append(
                {
                    "device_id": parse_int(columns[0]),
                    "name": columns[1],
                    "health": columns[2],
                    "memory_used_mb": used,
                    "memory_total_mb": total,
                    "memory_usage_percent": float(percent.group(1)) if percent else (round(used / total * 100, 2) if used and total else None),
                    "temperature_c": parse_int(columns[4]),
                    "utilization_percent": parse_int(columns[7]) if len(columns) > 7 else None,
                }
            )
    return {"devices": devices, "processes": processes}


def load_provider_output(provider: str) -> tuple[bool, str, str, int | None, float | None]:
    force_mock = os.environ.get("ACCELERATOR_OPS_FORCE_MOCK", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    if provider == "nvidia" and shutil.which("nvidia-smi") and not force_mock:
        returncode, stdout, stderr, latency = run_command(["nvidia-smi"])
        return returncode == 0, stdout or stderr, "nvidia-smi", returncode, latency
    if provider == "ascend" and shutil.which("npu-smi") and not force_mock:
        returncode, stdout, stderr, latency = run_command(["npu-smi", "info"])
        return returncode == 0, stdout or stderr, "npu-smi info", returncode, latency
    fixture = NVIDIA_FIXTURE if provider == "nvidia" else ASCEND_FIXTURE
    return True, read_text_file(fixture), f"mock fixture: {fixture.name}", None, None


def parse_provider_output(provider: str, output: str) -> dict[str, Any]:
    return parse_nvidia_smi(output) if provider == "nvidia" else parse_ascend_npu_smi(output)


def summarize(devices: list[dict[str, Any]], processes: list[dict[str, Any]]) -> dict[str, Any]:
    warnings: list[str] = []
    for device in devices:
        device_id = device.get("device_id")
        percent = device.get("memory_usage_percent")
        health = str(device.get("health") or "").upper()
        temp = device.get("temperature_c")
        if isinstance(percent, (int, float)) and percent >= 90:
            warnings.append(f"device {device_id} memory usage is high: {percent}%.")
        if health and health not in {"OK", "NORMAL"}:
            warnings.append(f"device {device_id} health is {health}.")
        if isinstance(temp, int) and temp >= 80:
            warnings.append(f"device {device_id} temperature is high: {temp}C.")
    return {
        "warnings": warnings,
        "max_memory_usage_percent": max(
            [item.get("memory_usage_percent") for item in devices if isinstance(item.get("memory_usage_percent"), (int, float))],
            default=None,
        ),
        "process_count": len(processes),
    }


def check_accelerator_status(provider: str = "auto") -> dict[str, Any]:
    selected = detect_provider(provider)
    ok, output, source, returncode, latency = load_provider_output(selected)
    parsed = parse_provider_output(selected, output)
    return {
        "ok": ok,
        "provider": selected,
        "source": source,
        "returncode": returncode,
        "latency_ms": latency,
        "summary": summarize(parsed["devices"], parsed["processes"]),
        "devices": parsed["devices"],
        "processes": parsed["processes"],
        "raw_excerpt": output[:2500],
    }


def check_accelerator_env(provider: str = "auto") -> dict[str, Any]:
    selected = detect_provider(provider)
    if selected == "nvidia":
        env_names = ["CUDA_HOME", "CUDA_PATH", "LD_LIBRARY_PATH", "PATH"]
        command = "nvidia-smi"
    else:
        env_names = ["ASCEND_HOME_PATH", "ASCEND_OPP_PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "PATH"]
        command = "npu-smi"
    snapshot = {name: os.environ.get(name, "") for name in env_names}
    missing = [name for name in env_names[:2] if not snapshot.get(name)]
    return {
        "ok": shutil.which(command) is not None and not missing,
        "provider": selected,
        "command_found": shutil.which(command) is not None,
        "missing": missing,
        "env_snapshot": snapshot,
        "suggestions": [] if shutil.which(command) else [f"{command} not found; check driver/runtime install or container device mounts."],
    }


def detect_memory_pressure(provider: str = "auto") -> dict[str, Any]:
    status = check_accelerator_status(provider)
    pressure = [
        device for device in status.get("devices", [])
        if isinstance(device.get("memory_usage_percent"), (int, float)) and device["memory_usage_percent"] >= 90
    ]
    status.update(
        {
            "memory_pressure": bool(pressure),
            "pressure_devices": pressure,
            "suggestions": [
                "Check residual processes before changing serving config.",
                "Lower gpu_memory_utilization/max_model_len or batch settings when model load OOM is confirmed.",
            ] if pressure else ["No high accelerator memory pressure detected."],
        }
    )
    return status


def list_accelerator_processes(provider: str = "auto") -> dict[str, Any]:
    status = check_accelerator_status(provider)
    return {
        "ok": status.get("ok", False),
        "provider": status.get("provider"),
        "source": status.get("source"),
        "processes": status.get("processes", []),
        "process_count": len(status.get("processes", [])),
    }
