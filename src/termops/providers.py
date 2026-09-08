"""Read-only environment probes used by the Monitor phase.

No provider in this module mutates the system. Network probes are limited to
loopback or explicitly allowed targets via the security layer.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from .models import EnvSnapshot


def check_port_available(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    probe_address = "127.0.0.1" if address == "0.0.0.0" else address
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_address, port))
        except OSError:
            return False
    return True


def capture_env_snapshot(
    *,
    cwd: str = "",
    language: str = "",
    env_allowlist: set[str] | None = None,
) -> EnvSnapshot:
    """Capture a safe, non-secret view of the local execution context."""
    allowlist = env_allowlist or {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "OS",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "NODE_PATH",
        "GO111MODULE",
        "RUST_BACKTRACE",
        "LANG",
    }
    raw_cwd = cwd or os.getcwd()
    path = os.environ.get("PATH", "")
    return EnvSnapshot(
        cwd=raw_cwd,
        shell=os.environ.get("SHELL", os.environ.get("COMSPEC", "")),
        path_entries=[entry for entry in path.split(os.pathsep) if entry],
        python_version=sys.version.replace("\n", " "),
        os_name=f"{platform.system()} {platform.release()}",
        language=language,
        env_vars={key: value for key, value in os.environ.items() if key in allowlist},
    )


def probe_command_availability(command: str) -> dict[str, Any]:
    """Check whether a command name is resolvable on PATH without running it."""
    if not command:
        return {"available": False, "resolved": None, "error": "empty command"}
    executable = shutil.which(command)
    return {
        "available": executable is not None,
        "resolved": executable,
        "error": None if executable else f"'{command}' was not found on PATH",
    }


def probe_python_module(module_name: str) -> dict[str, Any]:
    """Check whether a Python module can be imported in the current interpreter."""
    if not module_name:
        return {"available": False, "version": None, "error": "empty module name"}
    try:
        module = __import__(module_name)
        version = getattr(module, "__version__", None)
        return {"available": True, "version": version, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "version": None, "error": str(exc)}


def probe_path(path: str) -> dict[str, Any]:
    """Check whether a path exists and what kind of filesystem entry it is."""
    target = Path(path).expanduser()
    return {
        "path": str(target),
        "exists": target.exists(),
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "is_symlink": target.is_symlink(),
    }


class EnvProbe:
    """Aggregate read-only probe used by the analysis engine."""

    def __init__(self, env_allowlist: set[str] | None = None):
        self.env_allowlist = env_allowlist

    def snapshot(self, *, cwd: str = "", language: str = "") -> EnvSnapshot:
        return capture_env_snapshot(cwd=cwd, language=language, env_allowlist=self.env_allowlist)

    def capabilities(self) -> dict[str, Any]:
        return {
            "os": platform.system(),
            "os_release": platform.release(),
            "python": sys.version.replace("\n", " "),
            "python_executable": sys.executable,
            "cwd": os.getcwd(),
            "shell": shutil.which(os.environ.get("SHELL", "sh") or "sh") or None,
        }
