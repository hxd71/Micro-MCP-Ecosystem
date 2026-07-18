from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from aiops_agent.api import create_app
from aiops_agent.engine import OpsEngine
from aiops_agent.models import TargetRef, TaskKind, TaskStatus
from aiops_agent.web import stream_task_events


def auth(engine: OpsEngine) -> dict[str, str]:
    return {"X-AIOPS-Token": engine.operator_token}


def test_api_requires_operator_token(settings) -> None:
    engine = OpsEngine(settings)
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/v1/capabilities").status_code == 401
        assert client.get("/v1/capabilities", headers=auth(engine)).status_code == 200
    engine.store.close()


def test_web_login_diagnosis_and_csrf(settings, manifest_text: str) -> None:
    engine = OpsEngine(settings)
    app = create_app(settings, engine)
    with TestClient(app) as client:
        response = client.post("/v1/tasks/deploy", headers=auth(engine), json={"manifest": manifest_text})
        assert response.status_code == 202
        task_id = response.json()["id"]
        for _ in range(100):
            detail = client.get(f"/v1/tasks/{task_id}", headers=auth(engine)).json()
            if detail["task"]["status"] == "waiting_approval":
                break
            time.sleep(0.01)
        action = detail["actions"][0]
        client.post(
            f"/v1/actions/{action['id']}/decision",
            headers=auth(engine),
            json={"decision": "approve", "action_digest": action["digest"]},
        )
        for _ in range(100):
            detail = client.get(f"/v1/tasks/{task_id}", headers=auth(engine)).json()
            if detail["task"]["status"] == "succeeded":
                break
            time.sleep(0.01)

        login = client.post("/v1/web/login-code", headers=auth(engine)).json()
        code = login["url"].split("code=", 1)[1]
        logged_in = client.get(f"/login?code={code}", follow_redirects=True)
        assert logged_in.status_code == 200
        assert "运行总览" in logged_in.text
        csrf = logged_in.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]

        assert client.post("/ui-api/diagnose", json={"service": "qwen-test"}).status_code == 403
        diagnosed = client.post(
            "/ui-api/diagnose",
            headers={"X-CSRF-Token": csrf},
            json={"service": "qwen-test", "symptom": "503"},
        )
        assert diagnosed.status_code == 202
        diagnosed_id = diagnosed.json()["task_id"]
        for _ in range(100):
            diagnosed_detail = client.get(f"/v1/tasks/{diagnosed_id}", headers=auth(engine)).json()
            if diagnosed_detail["task"]["status"] in {"waiting_approval", "succeeded"}:
                break
            time.sleep(0.01)
        task_page = client.get(diagnosed.json()["location"])
        assert task_page.status_code == 200
        assert "Observation" in task_page.text
        if diagnosed_detail["actions"]:
            pending = diagnosed_detail["actions"][0]
            rejected = client.post(
                f"/ui-api/actions/{pending['id']}/decision",
                headers={"X-CSRF-Token": csrf},
                json={"decision": "reject", "action_digest": pending["digest"]},
            )
            assert rejected.status_code == 200
            assert rejected.json()["action"]["status"] == "rejected"
    engine.store.close()


def test_cancel_invalidates_pending_action(settings, manifest_text: str) -> None:
    engine = OpsEngine(settings)
    with TestClient(create_app(settings, engine)) as client:
        created = client.post(
            "/v1/tasks/deploy", headers=auth(engine), json={"manifest": manifest_text}
        ).json()
        for _ in range(100):
            detail = client.get(f"/v1/tasks/{created['id']}", headers=auth(engine)).json()
            if detail["task"]["status"] == "waiting_approval":
                break
            time.sleep(0.01)
        action = detail["actions"][0]

        cancelled = client.post(
            f"/v1/tasks/{created['id']}/cancel", headers=auth(engine)
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert engine.store.get_action(action["id"]).status.value == "rejected"

        replay = client.post(
            f"/v1/actions/{action['id']}/decision",
            headers=auth(engine),
            json={"decision": "approve", "action_digest": action["digest"]},
        )
        assert replay.status_code == 422
        assert "not waiting for approval" in replay.json()["detail"]
        assert engine.docker.inspect_service("qwen-test")["found"] is False
    engine.store.close()


class ConnectedOnce:
    def __init__(self) -> None:
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > 1


@pytest.mark.asyncio
async def test_sse_stream_emits_auditable_task_updates(settings) -> None:
    engine = OpsEngine(settings)
    task = engine.store.create_task(
        kind=TaskKind.DIAGNOSE,
        target=TargetRef(kind="service", name="qwen-test"),
        input_data={},
    )
    engine.store.update_task(task.id, TaskStatus.RUNNING)

    chunks = [
        chunk
        async for chunk in stream_task_events(
            ConnectedOnce(), engine.store, task.id, poll_seconds=0
        )
    ]

    payload = "".join(chunks)
    assert "event: audit" in payload
    assert '"event_type": "task.status"' in payload
    assert '"to": "running"' in payload
    assert payload.endswith(": keepalive\n\n")
    engine.store.close()
