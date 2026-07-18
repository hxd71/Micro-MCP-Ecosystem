from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Coroutine
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .config import Settings
from .diagnostics import config_findings, log_evidence, security_findings, tuned_memory_manifest
from .models import (
    TERMINAL_TASK_STATUSES,
    ActionStatus,
    ActionStep,
    ApprovalDecision,
    IncidentReport,
    InferenceServiceManifest,
    RiskLevel,
    ServiceRevision,
    Severity,
    TargetRef,
    Task,
    TaskKind,
    TaskStatus,
    utc_now,
)
from .providers import DockerOperations, NvidiaProvider, build_docker_provider, check_port_available
from .security import action_digest, new_token, validate_manifest_policy
from .store import StateStore


class OpsEngine:
    def __init__(
        self,
        settings: Settings,
        store: StateStore | None = None,
        docker_provider: DockerOperations | None = None,
        nvidia_provider: NvidiaProvider | None = None,
    ):
        self.settings = settings
        self.settings.ensure_directories()
        self.store = store or StateStore(settings.database_path)
        self.docker = docker_provider or build_docker_provider(settings)
        self.nvidia = nvidia_provider or NvidiaProvider(settings.profile)
        self._jobs: set[asyncio.Task[Any]] = set()
        self._monitor_task: asyncio.Task[Any] | None = None
        self.operator_token = self._load_or_create_operator_token()

    def _load_or_create_operator_token(self) -> str:
        path = self.settings.operator_token_path
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        token = new_token()
        path.write_text(token + "\n", encoding="utf-8")
        if self.settings.profile == "live" and Path(path).drive == "":
            path.chmod(0o640)
            try:
                grp_module: Any = importlib.import_module("grp")
                os_module: Any = os
                os_module.chown(path, -1, grp_module.getgrnam("aiops-operators").gr_gid)
            except (AttributeError, ImportError, KeyError, PermissionError):
                path.chmod(0o600)
        return token

    async def start(self) -> None:
        for task in self.store.list_tasks(limit=500):
            if task.status in {TaskStatus.EXECUTING, TaskStatus.VERIFYING}:
                self.store.update_task(task.id, TaskStatus.RECONCILING, force=True)
                self._spawn(self._reconcile_task(task.id))
            elif task.status == TaskStatus.QUEUED:
                self._spawn(self.process_task(task.id))
        if self.settings.monitor_enabled and self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="aiops-monitor")

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        job = asyncio.create_task(coroutine)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    def capabilities(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = shutil.disk_usage(self.settings.state_dir)
        return {
            "profile": self.settings.profile,
            "docker": self.docker.capabilities(),
            "nvidia": self.nvidia.status(),
            "trivy": {"available": shutil.which("trivy") is not None, "source": "PATH"},
            "host": {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "memory_percent": memory.percent,
                "memory_available_mb": round(memory.available / 1024 / 1024),
                "state_disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
            },
            "llm": {"enabled": False, "reason": "deterministic production core"},
            "event_chain_valid": self.store.verify_event_chain(),
        }

    def submit_deploy(self, manifest_text: str) -> Task:
        manifest = InferenceServiceManifest.from_yaml(manifest_text)
        task = self.store.create_task(
            TaskKind.DEPLOY,
            TargetRef(kind="service", name=manifest.metadata.name),
            {"manifest": manifest.model_dump(by_alias=True, mode="json")},
        )
        self._spawn(self.process_task(task.id))
        return task

    def submit_diagnosis(self, service_name: str, symptom: str = "") -> Task:
        self.store.get_service(service_name)
        task = self.store.create_task(
            TaskKind.DIAGNOSE,
            TargetRef(kind="service", name=service_name),
            {"symptom": symptom.strip()[:1000]},
        )
        self._spawn(self.process_task(task.id))
        return task

    def submit_security_scan(self, service_name: str) -> Task:
        self.store.get_service(service_name)
        task = self.store.create_task(
            TaskKind.SECURITY_SCAN, TargetRef(kind="service", name=service_name), {}
        )
        self._spawn(self.process_task(task.id))
        return task

    def submit_rollback(self, revision_id: str) -> Task:
        revision = self.store.get_revision(revision_id)
        task = self.store.create_task(
            TaskKind.ROLLBACK,
            TargetRef(kind="service", name=revision.service_name),
            {"revision_id": revision_id},
        )
        self._spawn(self.process_task(task.id))
        return task

    def cancel_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}:
            raise ValueError(f"task cannot be cancelled from {task.status.value}")
        for action in self.store.list_actions(task_id):
            if action.status == ActionStatus.PENDING:
                self.store.update_action(
                    action.id,
                    ActionStatus.REJECTED,
                    {"ok": False, "reason": "task cancelled by operator"},
                )
        return self.store.update_task(task_id, TaskStatus.CANCELLED)

    async def process_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task.status != TaskStatus.QUEUED:
            return
        self.store.update_task(task_id, TaskStatus.RUNNING)
        try:
            if task.kind == TaskKind.DEPLOY:
                await self._plan_deploy(task_id)
            elif task.kind in {TaskKind.DIAGNOSE, TaskKind.MONITOR}:
                await self._diagnose(task_id)
            elif task.kind == TaskKind.SECURITY_SCAN:
                await self._security_scan(task_id)
            elif task.kind == TaskKind.ROLLBACK:
                await self._plan_rollback(task_id)
            else:
                raise ValueError(f"unsupported task kind: {task.kind.value}")
        except Exception as exc:
            current = self.store.get_task(task_id)
            if current.status not in TERMINAL_TASK_STATUSES:
                self.store.update_task(task_id, TaskStatus.FAILED, error=str(exc), force=True)

    async def _plan_deploy(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        manifest = InferenceServiceManifest.model_validate(task.input["manifest"])
        policy_errors = validate_manifest_policy(manifest, self.settings)
        docker_capability = await asyncio.to_thread(self.docker.capabilities)
        gpu_status = await asyncio.to_thread(self.nvidia.status)
        image_present = (
            await asyncio.to_thread(self.docker.image_present, manifest.spec.image)
            if docker_capability.get("available")
            else False
        )
        existing = None
        with suppress(KeyError):
            existing = self.store.get_service(manifest.metadata.name)
        port_available = check_port_available(
            manifest.spec.endpoint.bind_address, manifest.spec.endpoint.host_port
        )
        if existing:
            port_available = True

        observations = [
            self.store.add_observation(
                task_id,
                "docker_capability",
                "Docker Engine API",
                "ok" if docker_capability.get("available") else "unavailable",
                "Docker daemon is available."
                if docker_capability.get("available")
                else "Docker daemon is unavailable.",
                docker_capability,
            ),
            self.store.add_observation(
                task_id,
                "nvidia_capability",
                gpu_status.get("source", "NVIDIA"),
                "ok" if gpu_status.get("available") else "unavailable",
                "NVIDIA devices were detected."
                if gpu_status.get("available")
                else "NVIDIA devices were not detected.",
                gpu_status,
            ),
            self.store.add_observation(
                task_id,
                "manifest_policy",
                "policy engine",
                "ok" if not policy_errors else "error",
                "Manifest passed policy checks."
                if not policy_errors
                else "Manifest violates deployment policy.",
                {"errors": policy_errors, "image_present": image_present, "port_available": port_available},
            ),
        ]
        errors = list(policy_errors)
        if not docker_capability.get("available"):
            errors.append(str(docker_capability.get("error", "Docker unavailable")))
        if not gpu_status.get("available"):
            errors.append(str(gpu_status.get("error", "NVIDIA unavailable")))
        selected = {int(item) for item in manifest.spec.gpu.device_ids}
        detected = {int(item["index"]) for item in gpu_status.get("devices", [])}
        if gpu_status.get("available") and not selected.issubset(detected):
            errors.append(f"selected GPU IDs are unavailable: {sorted(selected - detected)}")
        if not port_available:
            errors.append(f"host port is already in use: {manifest.spec.endpoint.host_port}")

        if errors:
            self.store.add_finding(
                task_id,
                "DEPLOYMENT_PREFLIGHT_FAILED",
                Severity.CRITICAL,
                1.0,
                "Deployment preflight failed",
                "; ".join(errors),
                [item.id for item in observations],
                "Resolve every preflight error before submitting a new manifest.",
            )
            report = self._build_report(task_id, "Deployment was not proposed because preflight failed.")
            self.store.update_task(task_id, TaskStatus.FAILED, report=report, error="preflight failed")
            return

        manifest_data = manifest.model_dump(by_alias=True, mode="json")
        manifest_digest = action_digest(manifest_data)
        pull = not image_present
        steps = []
        order = 1
        if pull:
            steps.append(
                ActionStep(order=order, action="pull_image", description=f"Pull {manifest.spec.image}")
            )
            order += 1
        if existing:
            steps.append(
                ActionStep(
                    order=order, action="stop_previous", description="Stop the active managed revision"
                )
            )
            order += 1
        steps.extend(
            [
                ActionStep(
                    order=order,
                    action="create_revision",
                    description="Create a policy-constrained vLLM container",
                ),
                ActionStep(
                    order=order + 1,
                    action="verify",
                    description="Wait for the configured vLLM health endpoint",
                ),
                ActionStep(
                    order=order + 2,
                    action="commit_or_rollback",
                    description="Commit the revision or restore the previous container",
                ),
            ]
        )
        self.store.create_action(
            task_id,
            "deploy",
            manifest.metadata.name,
            RiskLevel.HIGH,
            {"manifest": manifest_data, "manifest_digest": manifest_digest, "pull_image": pull},
            steps,
            [
                "Docker and NVIDIA capabilities remain available",
                "Model and secret paths remain within policy roots",
            ],
            [f"HTTP 200 from {manifest.spec.endpoint.health_path}", "Managed container remains running"],
            ["Stop and remove the candidate", "Restart the previous managed revision when one exists"],
            self.settings.approval_ttl_seconds,
        )
        self.store.update_task(
            task_id,
            TaskStatus.WAITING_APPROVAL,
            report=self._build_report(task_id, "Deployment is ready for approval."),
        )

    async def _diagnose(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        service = self.store.get_service(task.target.name)
        manifest = InferenceServiceManifest.model_validate(service["manifest"])
        container, logs, gpu, health = await asyncio.gather(
            asyncio.to_thread(self.docker.inspect_service, task.target.name),
            asyncio.to_thread(self.docker.service_logs, task.target.name, 200),
            asyncio.to_thread(self.nvidia.status),
            asyncio.to_thread(self.docker.verify_service, manifest, 3),
        )
        observations = {
            "container": self.store.add_observation(
                task_id,
                "container",
                "Docker Engine API",
                "ok" if container.get("found") and container.get("running") else "error",
                "Managed container is running."
                if container.get("running")
                else "Managed container is not running.",
                container,
            ),
            "logs": self.store.add_observation(
                task_id,
                "logs",
                "Docker logs",
                "ok" if logs.get("available") else "unavailable",
                "Recent bounded container logs were collected."
                if logs.get("available")
                else "Container logs are unavailable.",
                {
                    "matches": log_evidence(str(logs.get("logs", ""))),
                    "log_excerpt": str(logs.get("logs", ""))[-8000:],
                },
            ),
            "gpu": self.store.add_observation(
                task_id,
                "gpu",
                gpu.get("source", "NVIDIA"),
                "ok" if gpu.get("available") else "unavailable",
                "GPU metrics were collected." if gpu.get("available") else "GPU metrics are unavailable.",
                gpu,
            ),
            "health": self.store.add_observation(
                task_id,
                "service_health",
                "vLLM HTTP probe",
                "ok" if health.get("ok") else "error",
                "vLLM health endpoint returned HTTP 200."
                if health.get("ok")
                else "vLLM health verification failed.",
                health,
            ),
            "config": self.store.add_observation(
                task_id,
                "vllm_config",
                "InferenceService manifest",
                "ok",
                "Managed vLLM settings were evaluated.",
                manifest.spec.vllm.model_dump(by_alias=True),
            ),
        }
        if not health.get("ok"):
            code = "SERVICE_HTTP_5XX" if int(health.get("status", 0) or 0) >= 500 else "SERVICE_UNREACHABLE"
            self.store.add_finding(
                task_id,
                code,
                Severity.HIGH,
                1.0,
                "vLLM health check failed",
                f"Health evidence: {health}",
                [observations["health"].id],
                "Inspect logs and container state before restarting.",
            )
        if not container.get("running"):
            self.store.add_finding(
                task_id,
                "CONTAINER_NOT_RUNNING",
                Severity.CRITICAL,
                1.0,
                "Managed vLLM container is not running",
                f"Container state: {container}",
                [observations["container"].id],
                "Review the exit state and propose a managed restart or new revision.",
            )
        if container.get("oom_killed"):
            self.store.add_finding(
                task_id,
                "GPU_OUT_OF_MEMORY",
                Severity.CRITICAL,
                1.0,
                "Container was terminated by an out-of-memory condition",
                "Docker State.OOMKilled is true.",
                [observations["container"].id],
                "Lower vLLM memory pressure in a new revision.",
            )
        for match in observations["logs"].data.get("matches", []):
            self.store.add_finding(
                task_id,
                match["code"],
                Severity(match["severity"]),
                0.95,
                match["meaning"],
                f"Log line {match['line']}: {match['text']}",
                [observations["logs"].id],
                match["remediation"],
            )
        for device in gpu.get("devices", []):
            if not health.get("ok") and float(device.get("memory_percent", 0)) >= 90:
                self.store.add_finding(
                    task_id,
                    "GPU_MEMORY_PRESSURE",
                    Severity.HIGH,
                    0.95,
                    "GPU memory pressure is high",
                    f"GPU {device.get('index')} uses {device.get('memory_percent')}% memory.",
                    [observations["gpu"].id],
                    "Reduce vLLM cache/model pressure or free capacity outside the Agent.",
                )
        for item in config_findings(manifest):
            self.store.add_finding(task_id, evidence_ids=[observations["config"].id], **item)

        findings = self.store.list_findings(task_id)
        if findings and task.kind != TaskKind.MONITOR:
            critical_codes = {item.code for item in findings}
            payload: dict[str, Any] | None = None
            kind = ""
            steps: list[ActionStep] = []
            if critical_codes & {
                "GPU_OUT_OF_MEMORY",
                "GPU_MEMORY_PRESSURE",
                "VLLM_CACHE_BUDGET_EXHAUSTED",
                "VLLM_GPU_MEMORY_HEADROOM_LOW",
                "VLLM_CONTEXT_WINDOW_PRESSURE",
            }:
                tuned = tuned_memory_manifest(manifest, critical_codes, gpu)
                if tuned.canonical_json() != manifest.canonical_json():
                    tuned_data = tuned.model_dump(by_alias=True, mode="json")
                    payload = {
                        "manifest": tuned_data,
                        "pull_image": False,
                        "manifest_digest": action_digest(tuned_data),
                    }
                    kind = "tune_vllm"
                    steps = [
                        ActionStep(
                            order=1,
                            action="stop_previous",
                            description="Stop the active managed revision",
                        ),
                        ActionStep(
                            order=2,
                            action="create_revision",
                            description="Create a revision with capacity-bounded vLLM settings",
                        ),
                        ActionStep(order=3, action="verify", description="Verify vLLM health"),
                        ActionStep(
                            order=4,
                            action="commit_or_rollback",
                            description="Commit or automatically restore the previous revision",
                        ),
                    ]
            if payload is None and critical_codes & {
                "CONTAINER_NOT_RUNNING",
                "SERVICE_UNREACHABLE",
                "SERVICE_HTTP_5XX",
            }:
                payload = {"service_name": task.target.name}
                kind = "restart"
                steps = [
                    ActionStep(order=1, action="restart", description="Restart the active managed container"),
                    ActionStep(
                        order=2, action="verify", description="Verify the configured vLLM health endpoint"
                    ),
                ]
            if payload is not None:
                self.store.create_action(
                    task_id,
                    kind,
                    task.target.name,
                    RiskLevel.HIGH,
                    payload,
                    steps,
                    ["The target remains an Agent-managed container", "The approval digest still matches"],
                    ["HTTP health probe returns 200"],
                    [
                        "For a new revision, restore the previous container",
                        "For restart failure, leave the container stopped and report failure",
                    ],
                    self.settings.approval_ttl_seconds,
                )
                self.store.update_task(
                    task_id,
                    TaskStatus.WAITING_APPROVAL,
                    report=self._build_report(task_id, "Diagnosis found actionable issues."),
                )
            else:
                self.store.update_task(
                    task_id,
                    TaskStatus.SUCCEEDED,
                    report=self._build_report(
                        task_id, "Findings were recorded, but no safe bounded mutation was inferred."
                    ),
                )
        else:
            summary = (
                "No actionable issue was detected."
                if not findings
                else "Monitoring detected issues and opened an incident without mutation."
            )
            self.store.update_task(task_id, TaskStatus.SUCCEEDED, report=self._build_report(task_id, summary))

    async def _security_scan(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        service = self.store.get_service(task.target.name)
        manifest = InferenceServiceManifest.model_validate(service["manifest"])
        posture = await asyncio.to_thread(self.docker.security_posture, task.target.name)
        observation = self.store.add_observation(
            task_id,
            "container_security",
            "Docker inspect",
            "ok" if posture.get("available") and posture.get("found") else "unavailable",
            "Container security posture was inspected."
            if posture.get("found")
            else "Managed container was not found.",
            posture,
        )
        for item in security_findings(posture):
            self.store.add_finding(task_id, evidence_ids=[observation.id], **item)
        trivy_available = shutil.which("trivy") is not None
        trivy_result = (
            await asyncio.to_thread(self._run_trivy, manifest.spec.image)
            if trivy_available
            else {
                "available": False,
                "error": "trivy not found",
                "severity_counts": {},
            }
        )
        scan_observation = self.store.add_observation(
            task_id,
            "image_vulnerability_scan",
            "Trivy",
            "ok" if trivy_result.get("ok") else "warning",
            "Image vulnerability scan completed." if trivy_result.get("ok") else "scanner_unavailable",
            trivy_result,
        )
        if not trivy_result.get("ok"):
            self.store.add_finding(
                task_id,
                "SCANNER_UNAVAILABLE",
                Severity.INFO,
                1.0,
                "Image vulnerability scanner is unavailable",
                str(trivy_result.get("error", "Trivy scan failed")),
                [scan_observation.id],
                "Install/update the local Trivy database and repeat the scan; do not treat this as a pass.",
            )
        else:
            counts_value = trivy_result.get("severity_counts", {})
            counts: dict[str, Any] = counts_value if isinstance(counts_value, dict) else {}
            critical = int(counts.get("CRITICAL", 0))
            high = int(counts.get("HIGH", 0))
            if critical or high:
                self.store.add_finding(
                    task_id,
                    "IMAGE_VULNERABILITIES",
                    Severity.CRITICAL if critical else Severity.HIGH,
                    1.0,
                    "Container image contains high-severity vulnerabilities",
                    f"Trivy reported CRITICAL={critical}, HIGH={high} for the pinned image.",
                    [scan_observation.id],
                    "Select a patched image digest and submit a new deployment revision.",
                )
        findings = self.store.list_findings(task_id)
        summary = f"Security scan completed with {len(findings)} finding(s)."
        self.store.update_task(task_id, TaskStatus.SUCCEEDED, report=self._build_report(task_id, summary))

    @staticmethod
    def _run_trivy(image: str) -> dict[str, Any]:
        executable = shutil.which("trivy")
        if not executable:
            return {"ok": False, "available": False, "error": "trivy not found", "severity_counts": {}}
        try:
            process = subprocess.run(
                [
                    executable,
                    "image",
                    "--quiet",
                    "--skip-db-update",
                    "--scanners",
                    "vuln",
                    "--format",
                    "json",
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "available": True, "error": str(exc), "severity_counts": {}}
        if process.returncode != 0:
            return {
                "ok": False,
                "available": True,
                "error": process.stderr.strip()[:2000],
                "severity_counts": {},
            }
        try:
            import json

            payload = json.loads(process.stdout)
        except (ValueError, TypeError) as exc:
            return {
                "ok": False,
                "available": True,
                "error": f"invalid Trivy JSON: {exc}",
                "severity_counts": {},
            }
        counts: dict[str, int] = {}
        for result in payload.get("Results", []) or []:
            for vulnerability in result.get("Vulnerabilities", []) or []:
                severity = str(vulnerability.get("Severity", "UNKNOWN")).upper()
                counts[severity] = counts.get(severity, 0) + 1
        return {"ok": True, "available": True, "image": image, "severity_counts": counts}

    async def _plan_rollback(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        revision = self.store.get_revision(str(task.input["revision_id"]))
        self.store.create_action(
            task_id,
            "rollback",
            revision.service_name,
            RiskLevel.HIGH,
            {"revision_id": revision.id, "container_name": revision.container_name},
            [
                ActionStep(order=1, action="stop_current", description="Stop the current managed revision"),
                ActionStep(
                    order=2, action="activate_revision", description=f"Start {revision.container_name}"
                ),
                ActionStep(order=3, action="verify", description="Verify the restored service"),
            ],
            ["The requested revision still belongs to the target service"],
            ["HTTP health probe returns 200"],
            ["Restart the previously active revision if activation fails"],
            self.settings.approval_ttl_seconds,
        )
        self.store.update_task(
            task_id,
            TaskStatus.WAITING_APPROVAL,
            report=self._build_report(task_id, "Rollback is ready for approval."),
        )

    def decide_action(self, action_id: str, decision: ApprovalDecision) -> Any:
        pending = self.store.get_action(action_id)
        task = self.store.get_task(pending.task_id)
        if task.status != TaskStatus.WAITING_APPROVAL:
            raise ValueError(f"task is not waiting for approval: {task.status.value}")
        try:
            action = self.store.decide_action(action_id, decision.decision, decision.action_digest)
        except ValueError:
            expired = self.store.get_action(action_id)
            if expired.status == ActionStatus.EXPIRED:
                self.store.update_task(
                    expired.task_id, TaskStatus.FAILED, error="action approval expired", force=True
                )
            raise
        if decision.decision == "reject":
            self.store.update_task(action.task_id, TaskStatus.CANCELLED)
            return action
        self.store.update_task(action.task_id, TaskStatus.EXECUTING)
        self.store.update_action(action.id, ActionStatus.EXECUTING)
        self._spawn(self._execute_action(action.id))
        return self.store.get_action(action.id)

    async def _execute_action(self, action_id: str) -> None:
        action = self.store.get_action(action_id)
        try:
            if action.kind in {"deploy", "tune_vllm"}:
                await self._execute_deploy(action_id)
            elif action.kind == "restart":
                await self._execute_restart(action_id)
            elif action.kind == "rollback":
                await self._execute_rollback(action_id)
            else:
                raise ValueError(f"unsupported action kind: {action.kind}")
        except Exception as exc:
            self.store.update_action(action_id, ActionStatus.FAILED, {"ok": False, "error": str(exc)})
            self.store.update_task(action.task_id, TaskStatus.FAILED, error=str(exc), force=True)

    async def _execute_deploy(self, action_id: str) -> None:
        action = self.store.get_action(action_id)
        manifest = InferenceServiceManifest.model_validate(action.payload["manifest"])
        previous_service: dict[str, Any] | None = None
        with suppress(KeyError):
            previous_service = self.store.get_service(manifest.metadata.name)
        revision_id = uuid.uuid4().hex
        revision = ServiceRevision(
            id=revision_id,
            service_name=manifest.metadata.name,
            container_name=f"aiops-{manifest.metadata.name}-{revision_id[:8]}",
            image=manifest.spec.image,
            manifest_digest=action.payload["manifest_digest"],
            status="creating",
            created_at=utc_now(),
        )
        self.store.add_revision(revision)
        deployed = await asyncio.to_thread(
            self.docker.deploy_revision, manifest, revision_id, bool(action.payload.get("pull_image"))
        )
        self.store.update_task(action.task_id, TaskStatus.VERIFYING)
        verification = await asyncio.to_thread(
            self.docker.verify_service, manifest, manifest.spec.monitoring.startup_timeout_seconds
        )
        if verification.get("ok"):
            self.store.update_revision(revision_id, "active")
            self.store.upsert_service(
                manifest.metadata.name,
                manifest.model_dump(by_alias=True, mode="json"),
                action.payload["manifest_digest"],
                revision_id,
            )
            self.store.update_action(
                action_id, ActionStatus.SUCCEEDED, {"deployment": deployed, "verification": verification}
            )
            self.store.update_task(
                action.task_id,
                TaskStatus.SUCCEEDED,
                report=self._build_report(
                    action.task_id, "The approved vLLM revision is healthy and active."
                ),
            )
            return
        rollback = await asyncio.to_thread(
            self.docker.rollback_deployment, deployed["candidate_name"], deployed.get("previous_name")
        )
        rollback_verification: dict[str, Any] = {
            "ok": bool(rollback.get("ok")),
            "skipped": not bool(deployed.get("previous_name")),
        }
        if deployed.get("previous_name"):
            if previous_service is None:
                rollback_verification = {
                    "ok": False,
                    "error": "previous service manifest is unavailable",
                }
            else:
                previous_manifest = InferenceServiceManifest.model_validate(
                    previous_service["manifest"]
                )
                rollback_verification = await asyncio.to_thread(
                    self.docker.verify_service,
                    previous_manifest,
                    previous_manifest.spec.monitoring.startup_timeout_seconds,
                )
        rollback_ok = bool(rollback.get("ok") and rollback_verification.get("ok"))
        self.store.update_revision(revision_id, "rolled_back")
        self.store.update_action(
            action_id,
            ActionStatus.ROLLED_BACK if rollback_ok else ActionStatus.FAILED,
            {
                "deployment": deployed,
                "verification": verification,
                "rollback": rollback,
                "rollback_verification": rollback_verification,
            },
        )
        self.store.update_task(
            action.task_id,
            TaskStatus.ROLLED_BACK if rollback_ok else TaskStatus.FAILED,
            report=self._build_report(
                action.task_id,
                "Verification failed; the previous revision was restored and verified."
                if rollback_ok
                else "Verification failed and the rollback could not be verified.",
            ),
            error=None if rollback_ok else "rollback verification failed",
        )

    async def _execute_restart(self, action_id: str) -> None:
        action = self.store.get_action(action_id)
        result = await asyncio.to_thread(self.docker.restart_service, action.target)
        self.store.update_task(action.task_id, TaskStatus.VERIFYING)
        service = self.store.get_service(action.target)
        manifest = InferenceServiceManifest.model_validate(service["manifest"])
        verification = await asyncio.to_thread(
            self.docker.verify_service, manifest, manifest.spec.monitoring.startup_timeout_seconds
        )
        ok = bool(result.get("ok") and verification.get("ok"))
        self.store.update_action(
            action_id,
            ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED,
            {"restart": result, "verification": verification},
        )
        self.store.update_task(
            action.task_id,
            TaskStatus.SUCCEEDED if ok else TaskStatus.FAILED,
            report=self._build_report(
                action.task_id, "Restart completed." if ok else "Restart verification failed."
            ),
            error=None if ok else "restart verification failed",
        )

    async def _execute_rollback(self, action_id: str) -> None:
        action = self.store.get_action(action_id)
        revision = self.store.get_revision(action.payload["revision_id"])
        result = await asyncio.to_thread(
            self.docker.activate_container, revision.container_name, revision.service_name
        )
        self.store.update_task(action.task_id, TaskStatus.VERIFYING)
        service = self.store.get_service(revision.service_name)
        manifest = InferenceServiceManifest.model_validate(service["manifest"])
        verification = await asyncio.to_thread(
            self.docker.verify_service, manifest, manifest.spec.monitoring.startup_timeout_seconds
        )
        ok = bool(result.get("ok") and verification.get("ok"))
        if ok:
            self.store.update_revision(revision.id, "active")
            self.store.upsert_service(
                revision.service_name, service["manifest"], revision.manifest_digest, revision.id
            )
        self.store.update_action(
            action_id,
            ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED,
            {"activation": result, "verification": verification},
        )
        self.store.update_task(
            action.task_id,
            TaskStatus.SUCCEEDED if ok else TaskStatus.FAILED,
            report=self._build_report(
                action.task_id, "Requested revision restored." if ok else "Rollback verification failed."
            ),
            error=None if ok else "rollback verification failed",
        )

    async def _reconcile_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        container = await asyncio.to_thread(self.docker.inspect_service, task.target.name)
        if container.get("running"):
            self.store.update_task(task_id, TaskStatus.VERIFYING)
            service = self.store.get_service(task.target.name)
            manifest = InferenceServiceManifest.model_validate(service["manifest"])
            health = await asyncio.to_thread(self.docker.verify_service, manifest, 10)
            self.store.update_task(
                task_id,
                TaskStatus.SUCCEEDED if health.get("ok") else TaskStatus.FAILED,
                report=self._build_report(task_id, "Recovered task state was reconciled."),
                error=None if health.get("ok") else "reconciliation health check failed",
            )
        else:
            self.store.update_task(
                task_id, TaskStatus.FAILED, error="reconciliation found no running managed container"
            )

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.monitor_tick_seconds)
            for service in self.store.list_services():
                try:
                    manifest = InferenceServiceManifest.model_validate(service["manifest"])
                    last_checked = service.get("last_checked_at")
                    if last_checked:
                        elapsed = (
                            datetime.now(timezone.utc) - datetime.fromisoformat(last_checked)
                        ).total_seconds()
                        if elapsed < manifest.spec.monitoring.interval_seconds:
                            continue
                    health = await asyncio.to_thread(self.docker.verify_service, manifest, 3)
                    updated = self.store.update_service_health(service["name"], bool(health.get("ok")))
                    if int(updated["failure_count"]) >= manifest.spec.monitoring.failure_threshold:
                        active = [
                            task
                            for task in self.store.list_tasks(limit=100)
                            if task.target.name == service["name"]
                            and task.status not in TERMINAL_TASK_STATUSES
                        ]
                        if not active:
                            task = self.store.create_task(
                                TaskKind.MONITOR,
                                TargetRef(kind="service", name=service["name"]),
                                {"symptom": "automatic health threshold exceeded"},
                            )
                            self._spawn(self.process_task(task.id))
                except Exception as exc:
                    self.store.append_event("monitor.error", {"service": service["name"], "error": str(exc)})

    def task_detail(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "observations": [item.model_dump(mode="json") for item in self.store.list_observations(task_id)],
            "findings": [item.model_dump(mode="json") for item in self.store.list_findings(task_id)],
            "actions": [item.model_dump(mode="json") for item in self.store.list_actions(task_id)],
            "events": self.store.list_events(task_id),
        }

    def _build_report(self, task_id: str, summary: str) -> dict[str, Any]:
        observations = self.store.list_observations(task_id)
        findings = self.store.list_findings(task_id)
        actions = self.store.list_actions(task_id)
        task = self.store.get_task(task_id)
        report = IncidentReport(
            task_id=task_id,
            service=task.target.name,
            summary=summary,
            observation_ids=[item.id for item in observations],
            finding_ids=[item.id for item in findings],
            action_ids=[item.id for item in actions],
        )
        return report.model_dump(mode="json")
