from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "fixtures"


def demo_profile_enabled() -> bool:
    return os.environ.get("AIOPS_PROFILE", "live").strip().lower() == "demo"


def as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run_docker(args: list[str], timeout: int = 10) -> tuple[int, str, str, float]:
    started = time.perf_counter()
    process = subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=False)
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")
    return process.returncode, stdout, stderr, round((time.perf_counter() - started) * 1000, 2)


def load_mock_inspect(container: str) -> dict[str, Any] | None:
    path = FIXTURES_DIR / f"{container}.inspect.json"
    if path.exists():
        return json.loads(read_text_file(path))
    return None


def load_mock_logs(container: str) -> str | None:
    path = FIXTURES_DIR / f"{container}.logs.txt"
    if path.exists():
        return read_text_file(path)
    return None


def list_containers() -> dict[str, Any]:
    if docker_available() and not demo_profile_enabled():
        try:
            returncode, stdout, stderr, latency = run_docker(["ps", "-a", "--format", "{{json .}}"])
            containers = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            return {
                "ok": returncode == 0,
                "source": "docker ps -a",
                "latency_ms": latency,
                "containers": containers,
                "stderr": stderr[:1000],
            }
        except Exception as exc:
            return {"ok": False, "source": "docker ps -a", "error": str(exc)}
    mock = load_mock_inspect("vllm-qwen")
    return {
        "ok": True,
        "source": "mock fixture because docker is unavailable",
        "containers": [
            {
                "Names": "vllm-qwen",
                "Image": mock.get("Config", {}).get("Image") if mock else "vllm/vllm-openai:latest",
                "Status": mock.get("State", {}).get("Status") if mock else "running",
            }
        ],
    }


def inspect_container(container: str) -> dict[str, Any]:
    if not container.strip():
        return {"ok": False, "error": "container is required"}
    if docker_available() and not demo_profile_enabled():
        try:
            returncode, stdout, stderr, latency = run_docker(["inspect", container])
            if returncode == 0 and stdout.strip():
                data = json.loads(stdout)
                return {"ok": True, "source": "docker inspect", "latency_ms": latency, "inspect": data[0] if data else {}}
            return {"ok": False, "source": "docker inspect", "error": stderr.strip() or stdout.strip()}
        except Exception as exc:
            return {"ok": False, "source": "docker inspect", "error": str(exc)}
    mock = load_mock_inspect(container)
    if mock:
        return {"ok": True, "source": "mock inspect fixture", "inspect": mock}
    return {"ok": False, "source": "mock inspect fixture", "error": "container fixture not found and docker unavailable"}


def get_container_logs(container: str, lines: int = 100) -> dict[str, Any]:
    if not container.strip():
        return {"ok": False, "error": "container is required"}
    if docker_available() and not demo_profile_enabled():
        try:
            returncode, stdout, stderr, latency = run_docker(["logs", "--tail", str(max(1, min(lines, 1000))), container])
            return {"ok": returncode == 0, "source": "docker logs", "latency_ms": latency, "logs": stdout, "stderr": stderr[:1000]}
        except Exception as exc:
            return {"ok": False, "source": "docker logs", "error": str(exc)}
    logs = load_mock_logs(container)
    if logs is None:
        return {"ok": False, "source": "mock logs fixture", "error": "container log fixture not found and docker unavailable"}
    selected = "\n".join(logs.splitlines()[-max(1, min(lines, 1000)) :])
    return {"ok": True, "source": "mock logs fixture", "logs": selected}


def check_container_health(container: str) -> dict[str, Any]:
    inspection = inspect_container(container)
    if not inspection.get("ok"):
        return inspection
    data = inspection.get("inspect", {})
    state = data.get("State", {})
    health = state.get("Health", {})
    status = health.get("Status", state.get("Status", "unknown"))
    ok = state.get("Running", False) and status in {"healthy", "running"}
    return {
        "ok": ok,
        "source": inspection.get("source"),
        "container": container,
        "running": state.get("Running", False),
        "status": status,
        "health_log": health.get("Log", [])[-3:],
        "suggestions": [] if ok else ["Container is not healthy. Inspect logs, ports, mounts, and accelerator memory."],
    }


def check_container_ports(container: str) -> dict[str, Any]:
    inspection = inspect_container(container)
    if not inspection.get("ok"):
        return inspection
    data = inspection.get("inspect", {})
    ports = data.get("NetworkSettings", {}).get("Ports", {})
    return {"ok": bool(ports), "source": inspection.get("source"), "container": container, "ports": ports}


def check_container_mounts(container: str) -> dict[str, Any]:
    inspection = inspect_container(container)
    if not inspection.get("ok"):
        return inspection
    data = inspection.get("inspect", {})
    binds = data.get("HostConfig", {}).get("Binds", []) or []
    mounts = data.get("Mounts", []) or []
    warnings = []
    if not binds and not mounts:
        warnings.append("No mounts found; model path may not be visible inside the container.")
    return {"ok": not warnings, "source": inspection.get("source"), "container": container, "binds": binds, "mounts": mounts, "warnings": warnings}


def check_container_env(container: str, names: list[str] | str | None = None) -> dict[str, Any]:
    inspection = inspect_container(container)
    if not inspection.get("ok"):
        return inspection
    data = inspection.get("inspect", {})
    env_items = data.get("Config", {}).get("Env", []) or []
    env_map = {}
    for item in env_items:
        if "=" in item:
            key, value = item.split("=", 1)
            env_map[key] = value
    if names is None:
        requested: list[str] = []
    elif isinstance(names, str):
        requested = [item.strip() for item in names.replace(",", " ").split() if item.strip()]
    else:
        requested = [str(item).strip() for item in names if str(item).strip()]
    missing = [name for name in requested if not env_map.get(name)]
    return {"ok": not missing, "source": inspection.get("source"), "container": container, "env": env_map, "missing": missing}


def restart_container_dry_run(container: str) -> dict[str, Any]:
    return {
        "ok": True,
        "container": container,
        "proposed_command": f"docker restart {container}",
        "risk_level": "high",
        "will_execute": False,
        "required_checks_before_execution": [
            "Confirm no production traffic depends on this container.",
            "Confirm config backup exists if config changed.",
            "Confirm rollback path and logs are available.",
        ],
    }


def restart_container_with_approval(container: str, execute: bool = False, confirm_text: str = "") -> dict[str, Any]:
    plan = restart_container_dry_run(container)
    if not execute:
        plan.update({"executed": False, "approval_required": True, "note": "Dry-run only. Set execute=true after Hub approval."})
        return plan
    if confirm_text.strip() != container:
        return {"ok": False, "container": container, "executed": False, "error": "confirm_text must exactly match container."}
    allow_restart = os.environ.get("CONTAINER_OPS_ALLOW_RESTART", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    if not allow_restart:
        return {"ok": False, "container": container, "executed": False, "error": "Container restart execution is disabled. Set CONTAINER_OPS_ALLOW_RESTART=true only in controlled demos."}
    if not docker_available():
        return {"ok": False, "container": container, "executed": False, "error": "docker CLI is unavailable."}
    returncode, stdout, stderr, latency = run_docker(["restart", container], timeout=30)
    return {
        "ok": returncode == 0,
        "container": container,
        "executed": True,
        "returncode": returncode,
        "latency_ms": latency,
        "stdout": stdout[:2000],
        "stderr": stderr[:2000],
    }
