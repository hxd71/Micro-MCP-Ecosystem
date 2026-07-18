from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEMO_ROOT = BASE_DIR.parent / "demo"


def as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def resolve_path(value: str | None) -> Path:
    if not value:
        return Path.cwd().resolve()
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    candidates = [Path.cwd() / path, BASE_DIR.parent / path, DEMO_ROOT / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def run_command(args: list[str], timeout: int = 5) -> tuple[int, str, str]:
    process = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")
    return process.returncode, stdout, stderr


def check_process(keyword: str) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        return {"ok": False, "error": "keyword is required"}

    system = platform.system().lower()
    try:
        if system == "windows":
            returncode, stdout, stderr = run_command(["tasklist", "/fo", "csv"])
        else:
            returncode, stdout, stderr = run_command(["ps", "-eo", "pid,ppid,comm,args"])
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "source": "tasklist" if system == "windows" else "ps",
            "keyword": keyword,
            "error": "process listing timed out",
            "matched_count": 0,
            "matches": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": "tasklist" if system == "windows" else "ps",
            "keyword": keyword,
            "error": str(exc),
            "matched_count": 0,
            "matches": [],
        }

    matches = [line for line in stdout.splitlines() if keyword.lower() in line.lower()]
    return {
        "ok": returncode == 0,
        "source": "tasklist" if system == "windows" else "ps",
        "keyword": keyword,
        "matched_count": len(matches),
        "matches": matches[:20],
        "stderr": stderr[:1000],
    }


def check_port(port: int) -> dict[str, Any]:
    if port <= 0 or port > 65535:
        return {"ok": False, "error": "port must be between 1 and 65535"}

    listening_entries = [
        item for item in list_listening_ports().get("ports", []) if item.get("port") == port
    ]
    connectable = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connectable = True
    except OSError:
        connectable = False

    return {
        "ok": bool(listening_entries) or connectable,
        "port": port,
        "listening_entries": listening_entries,
        "tcp_connect_127_0_0_1": connectable,
        "suggestions": [] if listening_entries or connectable else ["Port is not listening on localhost."],
    }


def list_listening_ports() -> dict[str, Any]:
    system = platform.system().lower()
    commands = [["netstat", "-ano"]] if system == "windows" else [["ss", "-ltnp"], ["netstat", "-ltnp"]]
    last_error = ""
    output = ""
    source = ""
    for command in commands:
        try:
            returncode, stdout, stderr = run_command(command, timeout=6)
        except Exception as exc:
            last_error = str(exc)
            continue
        if returncode == 0 and stdout.strip():
            output = stdout
            source = " ".join(command)
            break
        last_error = stderr

    ports: list[dict[str, Any]] = []
    for line in output.splitlines():
        lower = line.lower()
        if not any(token in lower for token in ["listen", "listening"]):
            continue
        match = re.search(r"[:\.](\d+)\s", line)
        if not match:
            continue
        ports.append({"port": int(match.group(1)), "raw": line.strip()[:500]})

    return {
        "ok": bool(source),
        "source": source or "netstat/ss",
        "ports": ports[:200],
        "error": "" if source else last_error,
    }


def check_disk_usage(path: str = ".") -> dict[str, Any]:
    resolved = resolve_path(path)
    target = resolved if resolved.exists() else resolved.parent
    usage = shutil.disk_usage(target)
    percent = round(usage.used / usage.total * 100, 2) if usage.total else 0
    return {
        "ok": percent < 90,
        "source": str(target),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": percent,
        "warnings": [f"Disk usage is high: {percent}%"] if percent >= 90 else [],
    }


def check_memory_usage() -> dict[str, Any]:
    system = platform.system().lower()
    if system == "windows":
        try:
            returncode, stdout, stderr = run_command(
                ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json"],
                timeout=6,
            )
            data = json.loads(stdout) if returncode == 0 and stdout.strip() else {}
            total_kb = int(data.get("TotalVisibleMemorySize", 0))
            free_kb = int(data.get("FreePhysicalMemory", 0))
            used_kb = max(total_kb - free_kb, 0)
            percent = round(used_kb / total_kb * 100, 2) if total_kb else 0
            return {
                "ok": returncode == 0 and percent < 90,
                "source": "Win32_OperatingSystem",
                "total_bytes": total_kb * 1024,
                "used_bytes": used_kb * 1024,
                "free_bytes": free_kb * 1024,
                "used_percent": percent,
                "stderr": stderr[:1000],
            }
        except Exception as exc:
            return {"ok": False, "source": "Win32_OperatingSystem", "error": str(exc)}

    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return {"ok": False, "source": "/proc/meminfo", "error": "memory usage is unavailable on this platform"}
    values: dict[str, int] = {}
    for line in read_text_file(meminfo).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    total_kb = values.get("MemTotal", 0)
    available_kb = values.get("MemAvailable", values.get("MemFree", 0))
    used_kb = max(total_kb - available_kb, 0)
    percent = round(used_kb / total_kb * 100, 2) if total_kb else 0
    return {
        "ok": percent < 90,
        "source": "/proc/meminfo",
        "total_bytes": total_kb * 1024,
        "used_bytes": used_kb * 1024,
        "free_bytes": available_kb * 1024,
        "used_percent": percent,
    }


def check_env_vars(names: list[str] | str) -> dict[str, Any]:
    if isinstance(names, str):
        parsed_names = [item.strip() for item in re.split(r"[,;\s]+", names) if item.strip()]
    else:
        parsed_names = [str(item).strip() for item in names if str(item).strip()]
    snapshot = {name: os.environ.get(name, "") for name in parsed_names}
    missing = [name for name, value in snapshot.items() if not value]
    return {
        "ok": not missing,
        "source": "process environment",
        "env_snapshot": snapshot,
        "missing": missing,
    }


def tail_log(path: str, lines: int = 100) -> dict[str, Any]:
    resolved = resolve_path(path)
    if not resolved.exists():
        return {"ok": False, "source": str(resolved), "error": "log file not found"}
    content = read_text_file(resolved).splitlines()
    selected = content[-max(1, min(lines, 1000)) :]
    return {
        "ok": True,
        "source": str(resolved),
        "total_lines": len(content),
        "lines": [{"line": len(content) - len(selected) + idx + 1, "text": text} for idx, text in enumerate(selected)],
    }


def grep_log(path: str, pattern: str, max_matches: int = 20) -> dict[str, Any]:
    resolved = resolve_path(path)
    if not resolved.exists():
        return {"ok": False, "source": str(resolved), "error": "log file not found"}
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return {"ok": False, "source": str(resolved), "error": f"invalid regex: {exc}"}
    matches = []
    for line_no, text in enumerate(read_text_file(resolved).splitlines(), start=1):
        if regex.search(text):
            matches.append({"line": line_no, "text": text[:500]})
            if len(matches) >= max_matches:
                break
    return {
        "ok": True,
        "source": str(resolved),
        "pattern": pattern,
        "matched_count": len(matches),
        "matches": matches,
    }


def detect_recent_errors(log_path: str) -> dict[str, Any]:
    patterns = r"error|exception|traceback|oom|out of memory|timeout|failed|503|address already in use"
    result = grep_log(log_path, patterns, max_matches=50)
    result["error_patterns"] = patterns
    return result
