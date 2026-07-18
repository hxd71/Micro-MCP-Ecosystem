from __future__ import annotations

import ipaddress
import os
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 on Ubuntu 22.04
    import tomli as tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

Profile = Literal["live", "test", "demo"]


@dataclass(frozen=True)
class Settings:
    profile: Profile = "live"
    state_dir: Path = Path("/var/lib/aiops-agent")
    config_dir: Path = Path("/etc/aiops-agent")
    run_dir: Path = Path("/run/aiops-agent")
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    approval_ttl_seconds: int = 900
    allowed_model_roots: tuple[Path, ...] = (Path("/models"),)
    allowed_secret_roots: tuple[Path, ...] = (Path("/etc/aiops-agent/secrets"),)
    allowed_registries: tuple[str, ...] = ()
    allowed_probe_cidrs: tuple[str, ...] = ("127.0.0.0/8", "::1/128")
    monitor_enabled: bool = True
    monitor_tick_seconds: int = 15
    session_ttl_seconds: int = 8 * 60 * 60
    operator_name: str = "local-operator"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "agent.sock"

    @property
    def operator_token_path(self) -> Path:
        return self.state_dir / "operator.token"

    def validate(self) -> Settings:
        try:
            host = ipaddress.ip_address(self.web_host)
        except ValueError as exc:
            raise ValueError("web_host must be a loopback IP address") from exc
        if not host.is_loopback:
            raise ValueError("v1 Web UI refuses non-loopback bindings")
        if not 1 <= self.web_port <= 65535:
            raise ValueError("web_port must be between 1 and 65535")
        if self.profile not in {"live", "test", "demo"}:
            raise ValueError(f"unsupported profile: {self.profile}")
        return self

    @classmethod
    def load(cls, config_path: str | Path | None = None, profile: Profile | None = None) -> Settings:
        selected_profile = profile or os.environ.get("AIOPS_PROFILE", "live")
        if selected_profile not in {"live", "test", "demo"}:
            raise ValueError(f"unsupported profile: {selected_profile}")

        if os.name == "nt" or selected_profile in {"test", "demo"}:
            local_home = Path(os.environ.get("AIOPS_HOME", Path.cwd() / ".aiops")).resolve()
            settings = cls(
                profile=selected_profile,  # type: ignore[arg-type]
                state_dir=local_home,
                config_dir=local_home / "config",
                run_dir=local_home / "run",
                allowed_model_roots=((Path.cwd() / "demo" / "models").resolve(), Path.cwd().resolve()),
                allowed_secret_roots=((local_home / "secrets").resolve(),),
            )
        else:
            settings = cls(profile=selected_profile)  # type: ignore[arg-type]

        path = Path(config_path).resolve() if config_path else None
        if path and path.exists():
            with path.open("rb") as config_file:
                raw = tomllib.load(config_file)
            server = raw.get("server", {})
            policy = raw.get("policy", {})
            monitoring = raw.get("monitoring", {})
            settings = replace(
                settings,
                web_host=str(server.get("web_host", settings.web_host)),
                web_port=int(server.get("web_port", settings.web_port)),
                state_dir=Path(server.get("state_dir", settings.state_dir)).resolve(),
                run_dir=Path(server.get("run_dir", settings.run_dir)).resolve(),
                approval_ttl_seconds=int(policy.get("approval_ttl_seconds", settings.approval_ttl_seconds)),
                allowed_model_roots=tuple(
                    Path(item).resolve()
                    for item in policy.get("allowed_model_roots", settings.allowed_model_roots)
                ),
                allowed_secret_roots=tuple(
                    Path(item).resolve()
                    for item in policy.get("allowed_secret_roots", settings.allowed_secret_roots)
                ),
                allowed_registries=tuple(policy.get("allowed_registries", settings.allowed_registries)),
                allowed_probe_cidrs=tuple(policy.get("allowed_probe_cidrs", settings.allowed_probe_cidrs)),
                monitor_enabled=bool(monitoring.get("enabled", settings.monitor_enabled)),
                monitor_tick_seconds=int(monitoring.get("tick_seconds", settings.monitor_tick_seconds)),
                operator_name=str(raw.get("operator", {}).get("name", settings.operator_name)),
                extra=raw,
            )
        return settings.validate()

    def ensure_directories(self) -> None:
        # /etc is provisioned by installation and remains read-only to the daemon user.
        for path in (self.state_dir, self.run_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            # Operators need traversal to the protected token and Unix socket,
            # but must not be able to list daemon state or runtime contents.
            self.state_dir.chmod(0o711)
            self.run_dir.chmod(0o711)
            self.backup_dir.chmod(0o750)
