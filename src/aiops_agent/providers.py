from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from typing import Any, Protocol

import httpx

from .config import Settings
from .models import InferenceServiceManifest
from .security import ensure_allowed_path, redact, validate_probe_url


class DockerOperations(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def image_present(self, image: str) -> bool: ...
    def inspect_service(self, service_name: str) -> dict[str, Any]: ...
    def service_logs(self, service_name: str, lines: int = 200) -> dict[str, Any]: ...
    def security_posture(self, service_name: str) -> dict[str, Any]: ...
    def deploy_revision(
        self, manifest: InferenceServiceManifest, revision_id: str, pull: bool
    ) -> dict[str, Any]: ...
    def verify_service(
        self, manifest: InferenceServiceManifest, timeout_seconds: int = 45
    ) -> dict[str, Any]: ...
    def rollback_deployment(self, candidate_name: str, previous_name: str | None) -> dict[str, Any]: ...
    def restart_service(self, service_name: str) -> dict[str, Any]: ...
    def activate_container(self, container_name: str, service_name: str) -> dict[str, Any]: ...


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


def build_vllm_launch(manifest: InferenceServiceManifest) -> dict[str, Any]:
    """Build the bounded vLLM launch contract accepted by the Docker provider."""
    endpoint = manifest.spec.endpoint
    vllm = manifest.spec.vllm
    command = [
        "--model",
        manifest.spec.model.container_path,
        "--host",
        "0.0.0.0",
        "--port",
        str(endpoint.container_port),
        "--dtype",
        vllm.dtype,
        "--tensor-parallel-size",
        str(vllm.tensor_parallel_size),
        "--max-model-len",
        str(vllm.max_model_len),
        "--gpu-memory-utilization",
        str(vllm.gpu_memory_utilization),
        "--swap-space",
        str(vllm.swap_space_gib),
    ]
    if vllm.served_model_name:
        command.extend(["--served-model-name", vllm.served_model_name])
    if vllm.enforce_eager:
        command.append("--enforce-eager")
    if vllm.max_num_seqs is not None:
        command.extend(["--max-num-seqs", str(vllm.max_num_seqs)])
    if vllm.max_num_batched_tokens is not None:
        command.extend(["--max-num-batched-tokens", str(vllm.max_num_batched_tokens)])
    if vllm.disable_frontend_multiprocessing:
        command.append("--disable-frontend-multiprocessing")

    environment: dict[str, str] = {
        "DO_NOT_TRACK": "1",
        "VLLM_NO_USAGE_STATS": "1",
    }
    if vllm.engine_version != "auto":
        environment["VLLM_USE_V1"] = "1" if vllm.engine_version == "v1" else "0"
    entrypoint = (
        ["python", "-m", "vllm.entrypoints.openai.api_server"]
        if vllm.launch_mode == "python-module"
        else None
    )
    return {"command": command, "entrypoint": entrypoint, "environment": environment}


class NvidiaProvider:
    def __init__(self, profile: str = "live"):
        self.profile = profile

    def status(self) -> dict[str, Any]:
        if self.profile in {"demo", "test"}:
            return {
                "available": True,
                "source": f"explicit {self.profile} profile",
                "driver_version": "demo-550.54",
                "devices": [
                    {
                        "index": 0,
                        "name": "NVIDIA Demo GPU",
                        "memory_total_mb": 24576,
                        "memory_used_mb": 22118,
                        "memory_percent": 90.0,
                        "temperature_c": 72,
                    }
                ],
            }
        nvml_result = self._from_nvml()
        if nvml_result.get("available"):
            return nvml_result
        cli_result = self._from_nvidia_smi()
        if cli_result.get("available"):
            return cli_result
        return {
            "available": False,
            "source": "NVML and nvidia-smi",
            "error": f"{nvml_result.get('error', '')}; {cli_result.get('error', '')}".strip("; "),
            "devices": [],
        }

    def _from_nvml(self) -> dict[str, Any]:
        try:
            import pynvml

            pynvml.nvmlInit()
            devices: list[dict[str, Any]] = []
            for index in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
                used_mb = round(memory.used / 1024 / 1024)
                total_mb = round(memory.total / 1024 / 1024)
                devices.append(
                    {
                        "index": index,
                        "uuid": str(pynvml.nvmlDeviceGetUUID(handle)),
                        "name": str(pynvml.nvmlDeviceGetName(handle)),
                        "memory_total_mb": total_mb,
                        "memory_used_mb": used_mb,
                        "memory_percent": round(used_mb / total_mb * 100, 1) if total_mb else 0,
                        "temperature_c": pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU),
                    }
                )
            driver = str(pynvml.nvmlSystemGetDriverVersion())
            pynvml.nvmlShutdown()
            return {
                "available": bool(devices),
                "source": "NVML",
                "driver_version": driver,
                "devices": devices,
            }
        except Exception as exc:
            return {"available": False, "source": "NVML", "error": str(exc), "devices": []}

    def _from_nvidia_smi(self) -> dict[str, Any]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            return {
                "available": False,
                "source": "nvidia-smi",
                "error": "nvidia-smi not found",
                "devices": [],
            }
        try:
            process = subprocess.run(
                [
                    executable,
                    "--query-gpu=index,uuid,name,memory.total,memory.used,temperature.gpu,driver_version",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "source": "nvidia-smi", "error": str(exc), "devices": []}
        if process.returncode != 0:
            return {
                "available": False,
                "source": "nvidia-smi",
                "error": process.stderr.strip(),
                "devices": [],
            }
        devices: list[dict[str, Any]] = []
        driver = ""
        for line in process.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 7:
                continue
            total = int(float(parts[3]))
            used = int(float(parts[4]))
            driver = parts[6]
            devices.append(
                {
                    "index": int(parts[0]),
                    "uuid": parts[1],
                    "name": parts[2],
                    "memory_total_mb": total,
                    "memory_used_mb": used,
                    "memory_percent": round(used / total * 100, 1) if total else 0,
                    "temperature_c": int(float(parts[5])),
                }
            )
        return {
            "available": bool(devices),
            "source": "nvidia-smi",
            "driver_version": driver,
            "devices": devices,
        }


class DockerProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker

            client = docker.from_env(timeout=10)
            client.ping()
            self._client = client
            return client
        except Exception as exc:
            raise RuntimeError(f"Docker daemon is unavailable: {exc}") from exc

    def capabilities(self) -> dict[str, Any]:
        try:
            client = self._get_client()
            version = client.version()
            return {
                "available": True,
                "source": "Docker Engine API",
                "version": version.get("Version"),
                "api_version": version.get("ApiVersion"),
                "runtime": version.get("DefaultRuntime", "unknown"),
            }
        except RuntimeError as exc:
            return {"available": False, "source": "Docker Engine API", "error": str(exc)}

    def image_present(self, image: str) -> bool:
        try:
            self._get_client().images.get(image)
            return True
        except Exception:
            return False

    def _containers_for_service(self, service_name: str) -> list[Any]:
        client = self._get_client()
        return client.containers.list(all=True, filters={"label": f"aiops.service={service_name}"})

    def _active_container(self, service_name: str) -> Any | None:
        containers = self._containers_for_service(service_name)
        running = [container for container in containers if container.status == "running"]
        candidates = running or containers
        return (
            sorted(candidates, key=lambda item: item.attrs.get("Created", ""), reverse=True)[0]
            if candidates
            else None
        )

    def inspect_service(self, service_name: str) -> dict[str, Any]:
        try:
            container = self._active_container(service_name)
            if container is None:
                return {"available": True, "found": False, "service": service_name}
            container.reload()
            state = container.attrs.get("State", {})
            health = state.get("Health", {})
            return {
                "available": True,
                "found": True,
                "service": service_name,
                "container_id": container.short_id,
                "container_name": container.name,
                "status": state.get("Status", container.status),
                "running": bool(state.get("Running")),
                "health": health.get("Status", "not-configured"),
                "exit_code": state.get("ExitCode"),
                "oom_killed": bool(state.get("OOMKilled")),
                "started_at": state.get("StartedAt"),
                "image": container.attrs.get("Config", {}).get("Image"),
                "revision": container.labels.get("aiops.revision"),
            }
        except Exception as exc:
            return {"available": False, "found": False, "service": service_name, "error": str(exc)}

    def service_logs(self, service_name: str, lines: int = 200) -> dict[str, Any]:
        try:
            container = self._active_container(service_name)
            if container is None:
                return {"available": True, "found": False, "logs": ""}
            raw = container.logs(tail=max(1, min(lines, 1000)), timestamps=True)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            return {"available": True, "found": True, "container_name": container.name, "logs": text[-50000:]}
        except Exception as exc:
            return {"available": False, "found": False, "logs": "", "error": str(exc)}

    def security_posture(self, service_name: str) -> dict[str, Any]:
        try:
            container = self._active_container(service_name)
            if container is None:
                return {"available": True, "found": False, "checks": []}
            container.reload()
            attrs = container.attrs
            host = attrs.get("HostConfig", {})
            config = attrs.get("Config", {})
            mounts = attrs.get("Mounts", [])
            env_names = [str(item).split("=", 1)[0] for item in config.get("Env", []) or []]
            sensitive_mounts = []
            for mount in mounts:
                source = str(mount.get("Source", ""))
                if source in {"/", "/etc", "/proc", "/sys", "/var/run", "/var/run/docker.sock"} and mount.get(
                    "RW"
                ):
                    sensitive_mounts.append(source)
            bindings = host.get("PortBindings", {}) or {}
            public_ports = [
                port
                for port, values in bindings.items()
                if any((value.get("HostIp") or "0.0.0.0") in {"", "0.0.0.0", "::"} for value in values or [])
            ]
            return {
                "available": True,
                "found": True,
                "container_name": container.name,
                "checks": {
                    "privileged": bool(host.get("Privileged")),
                    "host_network": host.get("NetworkMode") == "host",
                    "host_pid": host.get("PidMode") == "host",
                    "read_only_rootfs": bool(host.get("ReadonlyRootfs")),
                    "no_new_privileges": "no-new-privileges:true" in (host.get("SecurityOpt") or []),
                    "cap_add": host.get("CapAdd") or [],
                    "user": config.get("User") or "root/default",
                    "sensitive_writable_mounts": sensitive_mounts,
                    "public_ports": public_ports,
                    "mutable_image_tag": "@sha256:" not in str(config.get("Image", "")),
                    "environment_names": env_names,
                },
            }
        except Exception as exc:
            return {"available": False, "found": False, "error": str(exc), "checks": {}}

    def deploy_revision(
        self, manifest: InferenceServiceManifest, revision_id: str, pull: bool
    ) -> dict[str, Any]:
        client = self._get_client()
        if not self.image_present(manifest.spec.image):
            if not pull:
                raise RuntimeError(
                    "image is not present and the approved proposal did not include an image pull"
                )
            client.images.pull(manifest.spec.image)

        service_name = manifest.metadata.name
        current = self._active_container(service_name)
        previous_name = current.name if current else None
        if current and current.status == "running":
            current.stop(timeout=20)

        model_path = ensure_allowed_path(manifest.spec.model.host_path, self.settings.allowed_model_roots)
        volumes: dict[str, dict[str, str]] = {
            str(model_path): {"bind": manifest.spec.model.container_path, "mode": "ro"}
        }
        for secret_ref in manifest.spec.secrets:
            secret_path = ensure_allowed_path(secret_ref.file, self.settings.allowed_secret_roots)
            volumes[str(secret_path)] = {"bind": f"/run/secrets/{secret_ref.name}", "mode": "ro"}

        endpoint = manifest.spec.endpoint
        launch = build_vllm_launch(manifest)

        try:
            from docker.types import DeviceRequest

            container_name = f"aiops-{service_name}-{revision_id[:8]}"
            container = client.containers.create(
                manifest.spec.image,
                name=container_name,
                command=launch["command"],
                entrypoint=launch["entrypoint"],
                environment=launch["environment"] or None,
                detach=True,
                labels={
                    "aiops.managed": "true",
                    "aiops.service": service_name,
                    "aiops.revision": revision_id,
                    "aiops.vllm.launch-mode": manifest.spec.vllm.launch_mode,
                },
                volumes=volumes,
                ports={f"{endpoint.container_port}/tcp": (endpoint.bind_address, endpoint.host_port)},
                device_requests=[
                    DeviceRequest(device_ids=manifest.spec.gpu.device_ids, capabilities=[["gpu"]])
                ],
                read_only=manifest.spec.security_context.read_only_root_filesystem,
                security_opt=["no-new-privileges:true"]
                if manifest.spec.security_context.no_new_privileges
                else None,
                cap_drop=["ALL"],
                tmpfs={
                    "/tmp": "rw,noexec,nosuid,size=4g",
                    "/root/.cache": "rw,nosuid,size=8g",
                    "/root/.config": "rw,noexec,nosuid,size=64m",
                    "/root/.triton": "rw,noexec,nosuid,size=64m",
                },
                shm_size="4g",
                restart_policy={"Name": "unless-stopped"},
            )
            container.start()
            return {
                "ok": True,
                "candidate_name": container_name,
                "candidate_id": container.short_id,
                "previous_name": previous_name,
            }
        except Exception:
            if current and previous_name:
                try:
                    current.start()
                except Exception:
                    pass
            raise

    def verify_service(self, manifest: InferenceServiceManifest, timeout_seconds: int = 45) -> dict[str, Any]:
        endpoint = manifest.spec.endpoint
        url = f"http://127.0.0.1:{endpoint.host_port}{endpoint.health_path}"
        validate_probe_url(url, self.settings)
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {"ok": False, "url": url, "error": "not attempted"}
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=3, follow_redirects=False)
                last = {
                    "ok": response.status_code == 200,
                    "url": url,
                    "status": response.status_code,
                    "body_excerpt": response.text[:1000],
                }
                if last["ok"]:
                    return redact(last)
            except httpx.HTTPError as exc:
                last = {"ok": False, "url": url, "error": str(exc)}
            time.sleep(2)
        return redact(last)

    def rollback_deployment(self, candidate_name: str, previous_name: str | None) -> dict[str, Any]:
        client = self._get_client()
        errors: list[str] = []
        try:
            candidate = client.containers.get(candidate_name)
            if candidate.status == "running":
                candidate.stop(timeout=15)
            candidate.remove(force=True)
        except Exception as exc:
            errors.append(f"candidate cleanup failed: {exc}")
        if previous_name:
            try:
                previous = client.containers.get(previous_name)
                previous.start()
            except Exception as exc:
                errors.append(f"previous revision restart failed: {exc}")
        return {"ok": not errors, "previous_name": previous_name, "errors": errors}

    def restart_service(self, service_name: str) -> dict[str, Any]:
        container = self._active_container(service_name)
        if container is None:
            return {"ok": False, "error": "managed container not found"}
        container.restart(timeout=20)
        return {"ok": True, "container_name": container.name}

    def activate_container(self, container_name: str, service_name: str) -> dict[str, Any]:
        client = self._get_client()
        current = self._active_container(service_name)
        if current and current.name != container_name and current.status == "running":
            current.stop(timeout=20)
        target = client.containers.get(container_name)
        target.start()
        return {"ok": True, "container_name": target.name, "previous_name": current.name if current else None}


class DemoDockerProvider:
    """Explicit fixture provider used only by demo/test profiles."""

    def __init__(self):
        self.services: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {"available": True, "source": "explicit demo provider", "version": "demo"}

    def image_present(self, image: str) -> bool:
        return True

    def inspect_service(self, service_name: str) -> dict[str, Any]:
        service = self.services.get(service_name)
        if not service:
            return {"available": True, "found": False, "service": service_name}
        return {
            "available": True,
            "found": True,
            "service": service_name,
            "running": service["running"],
            "status": "running" if service["running"] else "exited",
            "health": service.get("health", "healthy"),
            "container_name": service["container_name"],
            "oom_killed": service.get("oom_killed", False),
            "image": service["manifest"]["spec"]["image"],
            "revision": service["revision"],
        }

    def service_logs(self, service_name: str, lines: int = 200) -> dict[str, Any]:
        service = self.services.get(service_name)
        return {"available": True, "found": bool(service), "logs": service.get("logs", "") if service else ""}

    def security_posture(self, service_name: str) -> dict[str, Any]:
        service = self.services.get(service_name)
        if not service:
            return {"available": True, "found": False, "checks": {}}
        return {
            "available": True,
            "found": True,
            "checks": {
                "privileged": False,
                "host_network": False,
                "host_pid": False,
                "read_only_rootfs": True,
                "no_new_privileges": True,
                "cap_add": [],
                "user": "root/default",
                "sensitive_writable_mounts": [],
                "public_ports": [],
                "mutable_image_tag": "@sha256:" not in service["manifest"]["spec"]["image"],
                "environment_names": [],
            },
        }

    def deploy_revision(
        self, manifest: InferenceServiceManifest, revision_id: str, pull: bool
    ) -> dict[str, Any]:
        service_name = manifest.metadata.name
        previous = self.services.get(service_name)
        previous_name = previous["container_name"] if previous else None
        container_name = f"aiops-{service_name}-{revision_id[:8]}"
        self.services[service_name] = {
            "running": True,
            "health": "healthy",
            "container_name": container_name,
            "revision": revision_id,
            "manifest": manifest.model_dump(by_alias=True, mode="json"),
            "logs": "vLLM API server started\n",
            "previous": previous,
        }
        return {
            "ok": True,
            "candidate_name": container_name,
            "candidate_id": uuid.uuid4().hex[:12],
            "previous_name": previous_name,
        }

    def verify_service(self, manifest: InferenceServiceManifest, timeout_seconds: int = 45) -> dict[str, Any]:
        service = self.services.get(manifest.metadata.name)
        ok = bool(service and service.get("health") == "healthy")
        return {
            "ok": ok,
            "status": 200 if ok else 503,
            "url": f"http://127.0.0.1:{manifest.spec.endpoint.host_port}{manifest.spec.endpoint.health_path}",
        }

    def rollback_deployment(self, candidate_name: str, previous_name: str | None) -> dict[str, Any]:
        for name, service in list(self.services.items()):
            if service["container_name"] == candidate_name:
                previous = service.get("previous")
                if previous:
                    self.services[name] = previous
                else:
                    del self.services[name]
                return {"ok": True, "previous_name": previous_name, "errors": []}
        return {"ok": False, "previous_name": previous_name, "errors": ["candidate not found"]}

    def restart_service(self, service_name: str) -> dict[str, Any]:
        if service_name not in self.services:
            return {"ok": False, "error": "managed container not found"}
        self.services[service_name]["running"] = True
        self.services[service_name]["health"] = "healthy"
        return {"ok": True, "container_name": self.services[service_name]["container_name"]}

    def activate_container(self, container_name: str, service_name: str) -> dict[str, Any]:
        service = self.services.get(service_name)
        if not service:
            return {"ok": False, "error": "service not found"}
        service["running"] = True
        service["container_name"] = container_name
        return {"ok": True, "container_name": container_name}


def build_docker_provider(settings: Settings) -> DockerOperations:
    if settings.profile in {"demo", "test"}:
        return DemoDockerProvider()
    return DockerProvider(settings)
