from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskKind(StrEnum):
    DEPLOY = "deploy"
    DIAGNOSE = "diagnose"
    SECURITY_SCAN = "security_scan"
    ROLLBACK = "rollback"
    MONITOR = "monitor"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.ROLLED_BACK,
    TaskStatus.CANCELLED,
}


ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.VERIFYING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_APPROVAL: {TaskStatus.EXECUTING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.EXECUTING: {
        TaskStatus.VERIFYING,
        TaskStatus.RECONCILING,
        TaskStatus.FAILED,
        TaskStatus.ROLLED_BACK,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.ROLLED_BACK,
        TaskStatus.RECONCILING,
    },
    TaskStatus.RECONCILING: {
        TaskStatus.VERIFYING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.ROLLED_BACK,
    },
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.ROLLED_BACK: set(),
    TaskStatus.CANCELLED: set(),
}


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskLevel(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Metadata(StrictModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ModelMount(StrictModel):
    host_path: str = Field(alias="hostPath")
    container_path: str = Field(default="/model", alias="containerPath")

    @field_validator("container_path")
    @classmethod
    def validate_container_path(cls, value: str) -> str:
        if not value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("containerPath must be an absolute normalized path")
        return value


class GPUConfig(StrictModel):
    device_ids: list[str] = Field(default_factory=lambda: ["0"], alias="deviceIds", min_length=1)

    @field_validator("device_ids")
    @classmethod
    def validate_devices(cls, values: list[str]) -> list[str]:
        if any(not value.isdigit() for value in values):
            raise ValueError("GPU device IDs must be non-negative integers")
        return values


class EndpointConfig(StrictModel):
    bind_address: str = Field(default="127.0.0.1", alias="bindAddress")
    host_port: int = Field(default=8000, alias="hostPort", ge=1024, le=65535)
    container_port: int = Field(default=8000, alias="containerPort", ge=1, le=65535)
    health_path: str = Field(default="/v1/models", alias="healthPath")

    @field_validator("bind_address")
    @classmethod
    def validate_bind_address(cls, value: str) -> str:
        if value not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("bindAddress must be 127.0.0.1 or 0.0.0.0")
        return value

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value:
            raise ValueError("healthPath must be a path without query or fragment")
        return value


class VLLMConfig(StrictModel):
    launch_mode: Literal["image-entrypoint", "python-module"] = Field(
        default="image-entrypoint", alias="launchMode"
    )
    engine_version: Literal["auto", "v0", "v1"] = Field(default="auto", alias="engineVersion")
    dtype: Literal["auto", "half", "float16", "bfloat16", "float32"] = "auto"
    tensor_parallel_size: int = Field(default=1, alias="tensorParallelSize", ge=1, le=16)
    max_model_len: int = Field(default=8192, alias="maxModelLen", ge=128, le=262144)
    gpu_memory_utilization: float = Field(default=0.85, alias="gpuMemoryUtilization", gt=0.1, le=0.98)
    enforce_eager: bool = Field(default=False, alias="enforceEager")
    max_num_seqs: int | None = Field(default=None, alias="maxNumSeqs", ge=1, le=4096)
    max_num_batched_tokens: int | None = Field(
        default=None, alias="maxNumBatchedTokens", ge=128, le=262144
    )
    swap_space_gib: float = Field(default=4.0, alias="swapSpaceGiB", ge=0, le=64)
    disable_frontend_multiprocessing: bool = Field(
        default=False, alias="disableFrontendMultiprocessing"
    )
    served_model_name: str | None = Field(default=None, alias="servedModelName", max_length=128)

    @model_validator(mode="after")
    def validate_batch_budget(self) -> VLLMConfig:
        if self.max_num_batched_tokens is not None and self.max_num_batched_tokens < self.max_model_len:
            raise ValueError("maxNumBatchedTokens cannot be smaller than maxModelLen")
        return self


class SecretFile(StrictModel):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    file: str


class SecurityContext(StrictModel):
    read_only_root_filesystem: bool = Field(default=True, alias="readOnlyRootFilesystem")
    no_new_privileges: bool = Field(default=True, alias="noNewPrivileges")


class MonitoringConfig(StrictModel):
    interval_seconds: int = Field(default=60, alias="intervalSeconds", ge=15, le=3600)
    failure_threshold: int = Field(default=3, alias="failureThreshold", ge=1, le=20)
    startup_timeout_seconds: int = Field(
        default=180, alias="startupTimeoutSeconds", ge=15, le=1800
    )


class InferenceServiceSpec(StrictModel):
    image: str
    model: ModelMount
    gpu: GPUConfig = Field(default_factory=GPUConfig)
    endpoint: EndpointConfig = Field(default_factory=EndpointConfig)
    vllm: VLLMConfig = Field(default_factory=VLLMConfig)
    secrets: list[SecretFile] = Field(default_factory=list)
    security_context: SecurityContext = Field(default_factory=SecurityContext, alias="securityContext")
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not value.strip() or any(char.isspace() for char in value):
            raise ValueError("image must be a non-empty Docker image reference")
        return value


class InferenceServiceManifest(StrictModel):
    api_version: Literal["aiops.local/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["InferenceService"]
    metadata: Metadata
    spec: InferenceServiceSpec

    @model_validator(mode="after")
    def validate_parallelism(self) -> InferenceServiceManifest:
        if self.spec.vllm.tensor_parallel_size > len(self.spec.gpu.device_ids):
            raise ValueError("tensorParallelSize cannot exceed the number of selected GPUs")
        return self

    @classmethod
    def from_yaml(cls, text: str) -> InferenceServiceManifest:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("manifest root must be a mapping")
        return cls.model_validate(raw)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))


class TargetRef(StrictModel):
    kind: Literal["host", "service", "container"]
    name: str


class Task(StrictModel):
    id: str
    kind: TaskKind
    target: TargetRef
    status: TaskStatus
    input: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class Observation(StrictModel):
    id: str
    task_id: str
    kind: str
    source: str
    status: Literal["ok", "warning", "error", "unavailable"]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Finding(StrictModel):
    id: str
    task_id: str
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    title: str
    detail: str
    evidence_ids: list[str] = Field(min_length=1)
    remediation: str | None = None
    created_at: datetime


class ActionStep(StrictModel):
    order: int = Field(ge=1)
    action: str
    description: str


class ActionProposal(StrictModel):
    id: str
    task_id: str
    kind: Literal["deploy", "restart", "tune_vllm", "rollback"]
    target: str
    risk: RiskLevel
    payload: dict[str, Any]
    steps: list[ActionStep] = Field(min_length=1)
    preconditions: list[str]
    verification: list[str]
    rollback_plan: list[str]
    digest: str
    status: ActionStatus
    expires_at: datetime
    created_at: datetime
    result: dict[str, Any] | None = None


class ApprovalDecision(StrictModel):
    decision: Literal["approve", "reject"]
    action_digest: str = Field(min_length=64, max_length=64)


class ActionResult(StrictModel):
    action_id: str
    ok: bool
    rolled_back: bool = False
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class ServiceRevision(StrictModel):
    id: str
    service_name: str
    container_name: str
    image: str
    manifest_digest: str
    status: Literal["creating", "active", "stopped", "failed", "rolled_back"]
    created_at: datetime


class IncidentReport(StrictModel):
    task_id: str
    service: str
    summary: str
    observation_ids: list[str]
    finding_ids: list[str]
    action_ids: list[str]
    generated_at: datetime = Field(default_factory=utc_now)


SERVICE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
