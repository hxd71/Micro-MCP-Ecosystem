from __future__ import annotations

import asyncio
import sys

import pytest

from termops.engine import OpsEngine
from termops.models import ApprovalDecision, TaskStatus


async def wait_for_status(
    engine: OpsEngine, task_id: str, expected: set[TaskStatus], timeout: float = 3
) -> TaskStatus:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = engine.store.get_task(task_id).status
        if status in expected:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}: {engine.store.get_task(task_id).status}")


@pytest.mark.asyncio
async def test_analysis_task_proposes_and_executes_command(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ModuleNotFoundError: No module named 'requests'\n",
        source="stderr",
        language="python",
        command=f'"{sys.executable}" -c "print(123)"',
        cwd="",
        exit_code=1,
    )
    await wait_for_status(engine, task.id, {TaskStatus.WAITING_APPROVAL})
    action = engine.store.list_actions(task.id)[0]
    assert action.kind == "run_command"
    engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
    assert await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED}) == TaskStatus.SUCCEEDED
    result = engine.store.get_action(action.id).result
    assert result and result["ok"] is True
    # The suggested followup command prints the Python executable path
    assert "python" in result["stdout"].lower()
    assert result["returncode"] == 0
    assert engine.store.list_knowledge(task.id)
    phase_events = [event for event in engine.store.list_events(task.id) if event["event_type"] == "task.phase"]
    assert phase_events
    await engine.stop()


@pytest.mark.asyncio
async def test_analysis_finds_module_not_found(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ModuleNotFoundError: No module named 'click'",
        source="stderr",
        language="python",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    findings = engine.store.list_findings(task.id)
    codes = {f.code for f in findings}
    assert "MODULE_NOT_FOUND" in codes
    assert engine.store.verify_event_chain()
    await engine.stop()


@pytest.mark.asyncio
async def test_analysis_finds_command_not_found(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "'git' is not recognized as an internal or external command",
        source="stderr",
        language="",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    findings = engine.store.list_findings(task.id)
    codes = {f.code for f in findings}
    assert "COMMAND_NOT_FOUND" in codes
    await engine.stop()


@pytest.mark.asyncio
async def test_analysis_finds_permission_denied(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "PermissionError: [Errno 13] Permission denied: '/etc/config.ini'",
        source="stderr",
        language="python",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    findings = engine.store.list_findings(task.id)
    codes = {f.code for f in findings}
    assert "PERMISSION_DENIED" in codes
    await engine.stop()


@pytest.mark.asyncio
async def test_analysis_finds_connection_refused(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "requests.exceptions.ConnectionError: "
        "HTTPConnectionPool(host='localhost', port=8000): "
        "Max retries exceeded with url: /api (Caused by "
        "NewConnectionError('<urllib3.connection.HTTPConnection object>: "
        "Failed to establish a new connection: [Errno 111] Connection refused'))",
        source="stderr",
        language="python",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    findings = engine.store.list_findings(task.id)
    codes = {f.code for f in findings}
    assert "NETWORK_FAILURE" in codes or "CONNECTION_REFUSED" in codes
    await engine.stop()


@pytest.mark.asyncio
async def test_analysis_rejects_approval_decision(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ModuleNotFoundError: No module named 'requests'\n",
        source="stderr",
        language="python",
        command=f'"{sys.executable}" -c "print(123)"',
        cwd="",
        exit_code=1,
    )
    await wait_for_status(engine, task.id, {TaskStatus.WAITING_APPROVAL})
    action = engine.store.list_actions(task.id)[0]
    engine.decide_action(action.id, ApprovalDecision(decision="reject", action_digest=action.digest))
    terminal_statuses = {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}
    assert await wait_for_status(engine, task.id, terminal_statuses) in terminal_statuses
    action_after = engine.store.get_action(action.id)
    assert action_after.status.value == "rejected"
    await engine.stop()


@pytest.mark.asyncio
async def test_knowledge_recorded_after_analysis(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ModuleNotFoundError: No module named 'click'\n",
        source="stderr",
        language="python",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    task_status = engine.store.get_task(task.id).status
    if task_status == TaskStatus.WAITING_APPROVAL:
        actions = engine.store.list_actions(task.id)
        for action in actions:
            engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
        await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED})
    knowledge = engine.store.list_knowledge(task.id)
    assert len(knowledge) >= 1
    assert knowledge[0]["kind"] in {"analysis_summary", "command_result"}
    stats = engine.store.knowledge_stats()
    assert stats["total"] >= 1
    await engine.stop()


@pytest.mark.asyncio
async def test_knowledge_fts_search(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ModuleNotFoundError: No module named 'click'\n",
        source="stderr",
        language="python",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    task_status = engine.store.get_task(task.id).status
    if task_status == TaskStatus.WAITING_APPROVAL:
        for action in engine.store.list_actions(task.id):
            engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
        await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED})
    if engine.store._fts_enabled:
        results = engine.store.search_knowledge("exit code", limit=5)
        assert len(results) >= 1
    await engine.stop()


@pytest.mark.asyncio
async def test_event_chain_integrity(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_analysis(
        "ConnectionError: connection refused",
        source="stderr",
    )
    await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED, TaskStatus.WAITING_APPROVAL})
    assert engine.store.verify_event_chain()
    await engine.stop()
