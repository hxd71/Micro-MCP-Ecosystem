from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aiops_agent.models import ActionStep, RiskLevel, TargetRef, TaskKind, TaskStatus
from aiops_agent.store import StateStore


def test_task_transition_and_audit_chain(settings) -> None:
    store = StateStore(settings.database_path)
    task = store.create_task(TaskKind.DIAGNOSE, TargetRef(kind="service", name="qwen"), {})
    store.update_task(task.id, TaskStatus.RUNNING)
    store.update_task(task.id, TaskStatus.SUCCEEDED, report={"summary": "ok"})
    assert store.get_task(task.id).status == TaskStatus.SUCCEEDED
    assert store.verify_event_chain()
    with pytest.raises(ValueError, match="invalid task transition"):
        store.update_task(task.id, TaskStatus.RUNNING)


def test_action_digest_is_single_use_and_bound(settings) -> None:
    store = StateStore(settings.database_path)
    task = store.create_task(TaskKind.DEPLOY, TargetRef(kind="service", name="qwen"), {})
    store.update_task(task.id, TaskStatus.RUNNING)
    action = store.create_action(
        task.id,
        "deploy",
        "qwen",
        RiskLevel.HIGH,
        {"image": "pinned"},
        [ActionStep(order=1, action="deploy", description="deploy")],
        ["preflight"],
        ["health"],
        ["rollback"],
        900,
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        store.decide_action(action.id, "approve", "0" * 64)
    approved = store.decide_action(action.id, "approve", action.digest)
    assert approved.status.value == "approved"
    with pytest.raises(ValueError, match="not pending"):
        store.decide_action(action.id, "approve", action.digest)


def test_expired_approval_is_rejected(settings) -> None:
    store = StateStore(settings.database_path)
    task = store.create_task(TaskKind.DEPLOY, TargetRef(kind="service", name="qwen"), {})
    action = store.create_action(
        task.id,
        "deploy",
        "qwen",
        RiskLevel.HIGH,
        {},
        [ActionStep(order=1, action="deploy", description="deploy")],
        [],
        [],
        [],
        -1,
    )
    assert action.expires_at < datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="expired"):
        store.decide_action(action.id, "approve", action.digest)
