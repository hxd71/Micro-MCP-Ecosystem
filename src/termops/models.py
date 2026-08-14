from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskKind(StrEnum):
    """High-level intent submitted by the operator."""

    ANALYZE = "analyze"  # analyze terminal output / code error / stack trace
    VERIFY = "verify"  # verify a previously proposed fix or hypothesis
    PROBE = "probe"  # read-only environment context collection
    KNOWLEDGE_RECORD = "knowledge_record"  # explicit knowledge-base write


class TaskStatus(StrEnum):
    """Workflow status of a task through the MAPE-K loop."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MapePhase(StrEnum):
    """Current phase inside the MAPE-K control loop."""

    MONITOR = "monitor"
    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    KNOWLEDGE = "knowledge"


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
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
        TaskStatus.CANCELLED,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.RECONCILING,
    },
    TaskStatus.RECONCILING: {
        TaskStatus.VERIFYING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
    },
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TargetRef(StrictModel):
    kind: Literal["terminal", "workspace", "file", "command"]
    name: str = Field(min_length=1, max_length=128)


class FileAttachment(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=200_000)
    kind: Literal["stdout", "stderr", "log", "config", "other"] = "other"


class EnvSnapshot(StrictModel):
    cwd: str = ""
    shell: str = ""
    path_entries: list[str] = Field(default_factory=list)
    python_version: str = ""
    os_name: str = ""
    language: str = ""
    env_vars: dict[str, str] = Field(default_factory=dict)


class AnalysisRequest(StrictModel):
    """Input to an ANALYZE or VERIFY task."""

    text: str = Field(default="", max_length=200_000)
    source: str = Field(default="stdin", max_length=64)
    language: str = Field(default="", max_length=64)
    command: str = Field(default="", max_length=2000)
    cwd: str = Field(default="", max_length=2000)
    exit_code: int | None = Field(default=None, ge=-4096, le=4096)
    files: list[FileAttachment] = Field(default_factory=list)
    env: EnvSnapshot = Field(default_factory=EnvSnapshot)
    history_task_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("files", mode="after")
    @classmethod
    def limit_files(cls, value: list[FileAttachment]) -> list[FileAttachment]:
        if len(value) > 5:
            raise ValueError("at most 5 files may be attached")
        return value


class Task(StrictModel):
    id: str
    kind: TaskKind
    phase: MapePhase = MapePhase.MONITOR
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
    kind: str
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
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class LLMAttribution(StrictModel):
    """Structured error attribution produced by an optional LLM pass."""

    primary_cause: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.0, ge=0, le=1)
    remediation_steps: list[str] = Field(default_factory=list, max_length=10)
    proposed_command: str | None = Field(default=None, max_length=2000)
    needs_approval: bool = True
    safety_notes: list[str] = Field(default_factory=list, max_length=10)


class AnalysisReport(StrictModel):
    task_id: str
    summary: str
    observation_count: int
    finding_count: int
    action_ids: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    llm_attribution: LLMAttribution | None = None
    retrieved_knowledge_ids: list[str] = Field(default_factory=list)


# Convenience regex kept here because it is part of the task/target contract.
TARGET_NAME_RE = re.compile(r"^[a-zA-Z0-9_.@:+#~/-]{1,128}$")