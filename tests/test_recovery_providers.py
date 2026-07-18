from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from aiops_agent.engine import OpsEngine
from aiops_agent.models import InferenceServiceManifest, TargetRef, TaskKind, TaskStatus
from aiops_agent.providers import DemoDockerProvider, NvidiaProvider
from aiops_agent.security import action_digest


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


class UnavailableDockerProvider(DemoDockerProvider):
    def capabilities(self):
        return {
            "available": False,
            "source": "Docker Engine API",
            "error": "Docker daemon is unavailable: injected test failure",
        }

    def image_present(self, image: str) -> bool:
        raise AssertionError("image lookup must not run when Docker is unavailable")

    def deploy_revision(self, manifest, revision_id: str, pull: bool):
        raise AssertionError("deployment must not run after a failed preflight")


@pytest.mark.asyncio
async def test_docker_unavailable_fails_read_only_preflight(settings, manifest_text: str) -> None:
    provider = UnavailableDockerProvider()
    engine = OpsEngine(settings, docker_provider=provider)

    task = engine.submit_deploy(manifest_text)
    assert await wait_for_status(engine, task.id, {TaskStatus.FAILED}) == TaskStatus.FAILED

    detail = engine.task_detail(task.id)
    assert detail["actions"] == []
    assert [item["code"] for item in detail["findings"]] == ["DEPLOYMENT_PREFLIGHT_FAILED"]
    assert any(item["kind"] == "docker_capability" for item in detail["observations"])
    assert provider.services == {}
    await engine.stop()
    engine.store.close()


@pytest.mark.asyncio
async def test_daemon_start_reconciles_persisted_executing_task(
    settings, manifest_text: str
) -> None:
    provider = DemoDockerProvider()
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    manifest_data = manifest.model_dump(by_alias=True, mode="json")
    revision_id = "a" * 32
    provider.deploy_revision(manifest, revision_id, pull=False)

    first = OpsEngine(settings, docker_provider=provider)
    first.store.upsert_service(
        manifest.metadata.name,
        manifest_data,
        action_digest(manifest_data),
        revision_id,
    )
    task = first.store.create_task(
        TaskKind.DEPLOY,
        TargetRef(kind="service", name=manifest.metadata.name),
        {"manifest": manifest_data},
    )
    first.store.update_task(task.id, TaskStatus.EXECUTING, force=True)
    first.store.close()

    recovered = OpsEngine(settings, docker_provider=provider)
    await recovered.start()
    assert await wait_for_status(recovered, task.id, {TaskStatus.SUCCEEDED}) == TaskStatus.SUCCEEDED
    transitions = [
        event["data"].get("to")
        for event in recovered.store.list_events(task.id)
        if event["event_type"] == "task.status"
    ]
    assert TaskStatus.RECONCILING.value in transitions
    assert TaskStatus.VERIFYING.value in transitions
    assert recovered.store.verify_event_chain()
    await recovered.stop()
    recovered.store.close()


@pytest.mark.asyncio
async def test_security_scan_reports_missing_scanner(
    settings, manifest_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DemoDockerProvider()
    manifest = InferenceServiceManifest.from_yaml(manifest_text)
    manifest_data = manifest.model_dump(by_alias=True, mode="json")
    provider.deploy_revision(manifest, "b" * 32, pull=False)
    engine = OpsEngine(settings, docker_provider=provider)
    engine.store.upsert_service(
        manifest.metadata.name,
        manifest_data,
        action_digest(manifest_data),
        "b" * 32,
    )
    monkeypatch.setattr("aiops_agent.engine.shutil.which", lambda _name: None)

    task = engine.submit_security_scan(manifest.metadata.name)
    assert await wait_for_status(engine, task.id, {TaskStatus.SUCCEEDED}) == TaskStatus.SUCCEEDED

    detail = engine.task_detail(task.id)
    assert any(item["summary"] == "scanner_unavailable" for item in detail["observations"])
    assert any(item["code"] == "SCANNER_UNAVAILABLE" for item in detail["findings"])
    await engine.stop()
    engine.store.close()


def test_nvidia_smi_is_a_fixed_argv_read_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NvidiaProvider("live")
    monkeypatch.setattr(
        provider,
        "_from_nvml",
        lambda: {"available": False, "source": "NVML", "error": "NVML unavailable", "devices": []},
    )
    monkeypatch.setattr("aiops_agent.providers.shutil.which", lambda name: f"/usr/bin/{name}")
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="0, GPU-123, NVIDIA RTX Test, 4096, 1024, 55, 550.54\n",
            stderr="",
        )

    monkeypatch.setattr("aiops_agent.providers.subprocess.run", fake_run)
    status = provider.status()

    assert status["available"] is True
    assert status["source"] == "nvidia-smi"
    assert status["devices"][0]["memory_percent"] == 25.0
    assert captured["argv"] == [
        "/usr/bin/nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.used,temperature.gpu,driver_version",
        "--format=csv,noheader,nounits",
    ]
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": 8,
        "check": False,
    }
    assert not isinstance(captured["argv"], str)
    assert subprocess.list2cmdline(captured["argv"]).startswith("/usr/bin/nvidia-smi ")
