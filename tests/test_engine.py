from __future__ import annotations

import asyncio

import pytest

from aiops_agent.engine import OpsEngine
from aiops_agent.models import ApprovalDecision, TaskStatus
from aiops_agent.providers import DemoDockerProvider


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
async def test_deploy_waits_for_approval_then_executes(settings, manifest_text: str) -> None:
    engine = OpsEngine(settings)
    task = engine.submit_deploy(manifest_text)
    assert (
        await wait_for_status(engine, task.id, {TaskStatus.WAITING_APPROVAL}) == TaskStatus.WAITING_APPROVAL
    )
    action = engine.store.list_actions(task.id)[0]
    engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
    assert await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED}) == TaskStatus.SUCCEEDED
    assert engine.store.get_service("qwen-test")["active_revision"]
    assert engine.store.verify_event_chain()
    await engine.stop()


class FailingHealthProvider(DemoDockerProvider):
    def verify_service(self, manifest, timeout_seconds: int = 45):
        return {"ok": False, "status": 503, "error": "injected verification failure"}


class FailingCandidateProvider(DemoDockerProvider):
    def verify_service(self, manifest, timeout_seconds: int = 45):
        if manifest.spec.vllm.served_model_name == "broken-candidate":
            return {"ok": False, "status": 503, "error": "candidate is unhealthy"}
        return super().verify_service(manifest, timeout_seconds)


@pytest.mark.asyncio
async def test_failed_verification_executes_preapproved_rollback(settings, manifest_text: str) -> None:
    provider = FailingHealthProvider()
    engine = OpsEngine(settings, docker_provider=provider)
    task = engine.submit_deploy(manifest_text)
    await wait_for_status(engine, task.id, {TaskStatus.WAITING_APPROVAL})
    action = engine.store.list_actions(task.id)[0]
    engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
    assert await wait_for_status(engine, task.id, {TaskStatus.ROLLED_BACK}) == TaskStatus.ROLLED_BACK
    assert engine.store.get_action(action.id).status.value == "rolled_back"
    assert provider.services == {}
    await engine.stop()


@pytest.mark.asyncio
async def test_failed_update_keeps_original_manifest_and_verifies_rollback(
    settings, manifest_text: str
) -> None:
    provider = FailingCandidateProvider()
    engine = OpsEngine(settings, docker_provider=provider)
    first = engine.submit_deploy(manifest_text)
    await wait_for_status(engine, first.id, {TaskStatus.WAITING_APPROVAL})
    first_action = engine.store.list_actions(first.id)[0]
    engine.decide_action(
        first_action.id,
        ApprovalDecision(decision="approve", action_digest=first_action.digest),
    )
    await wait_for_status(engine, first.id, {TaskStatus.SUCCEEDED})
    original_manifest = engine.store.get_service("qwen-test")["manifest"]

    broken_text = manifest_text.replace(
        "    gpuMemoryUtilization: 0.85",
        "    gpuMemoryUtilization: 0.85\n    servedModelName: broken-candidate",
    )
    update = engine.submit_deploy(broken_text)
    await wait_for_status(engine, update.id, {TaskStatus.WAITING_APPROVAL})
    assert engine.store.get_service("qwen-test")["manifest"] == original_manifest

    update_action = engine.store.list_actions(update.id)[0]
    engine.decide_action(
        update_action.id,
        ApprovalDecision(decision="approve", action_digest=update_action.digest),
    )
    await wait_for_status(engine, update.id, {TaskStatus.ROLLED_BACK})

    result = engine.store.get_action(update_action.id).result
    assert result and result["rollback_verification"]["ok"] is True
    assert engine.store.get_service("qwen-test")["manifest"] == original_manifest
    assert provider.services["qwen-test"]["manifest"] == original_manifest
    await engine.stop()


@pytest.mark.asyncio
async def test_diagnosis_findings_reference_observations(settings, manifest_text: str) -> None:
    provider = DemoDockerProvider()
    engine = OpsEngine(settings, docker_provider=provider)
    deploy = engine.submit_deploy(manifest_text)
    await wait_for_status(engine, deploy.id, {TaskStatus.WAITING_APPROVAL})
    action = engine.store.list_actions(deploy.id)[0]
    engine.decide_action(action.id, ApprovalDecision(decision="approve", action_digest=action.digest))
    await wait_for_status(engine, deploy.id, {TaskStatus.SUCCEEDED})
    provider.services["qwen-test"]["oom_killed"] = True
    provider.services["qwen-test"]["health"] = "failed"
    diagnosis = engine.submit_diagnosis("qwen-test", "GPU memory is high")
    await wait_for_status(engine, diagnosis.id, {TaskStatus.WAITING_APPROVAL, TaskStatus.SUCCEEDED})
    observation_ids = {item.id for item in engine.store.list_observations(diagnosis.id)}
    findings = engine.store.list_findings(diagnosis.id)
    assert findings
    assert all(set(finding.evidence_ids).issubset(observation_ids) for finding in findings)
    await engine.stop()
