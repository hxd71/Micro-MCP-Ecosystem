from __future__ import annotations

from fastapi.testclient import TestClient

from aiops_agent.api import create_app
from aiops_agent.engine import OpsEngine
from aiops_agent.models import InferenceServiceManifest


def test_management_api_route_contract(settings) -> None:
    engine = OpsEngine(settings)
    app = create_app(settings, engine)
    routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}
    expected = {
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/services"),
        ("POST", "/v1/tasks/deploy"),
        ("POST", "/v1/tasks/diagnose"),
        ("POST", "/v1/tasks/security"),
        ("POST", "/v1/tasks/rollback"),
        ("GET", "/v1/tasks/{task_id}"),
        ("GET", "/v1/tasks/{task_id}/events"),
        ("POST", "/v1/tasks/{task_id}/cancel"),
        ("POST", "/v1/actions/{action_id}/decision"),
        ("GET", "/v1/events"),
    }
    assert expected.issubset(routes)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True, "profile": "test"}
    engine.store.close()


def test_manifest_json_schema_contract() -> None:
    schema = InferenceServiceManifest.model_json_schema(by_alias=True)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata", "spec"}
    spec = schema["$defs"]["InferenceServiceSpec"]
    assert spec["additionalProperties"] is False
    assert {"image", "model"}.issubset(spec["required"])
    assert "vllm" in spec["properties"]
    assert "monitoring" in spec["properties"]
