from __future__ import annotations

from fastapi.testclient import TestClient

from termops.api import create_app
from termops.engine import OpsEngine
from termops.models import AnalysisRequest, TaskKind


def test_management_api_route_contract(settings) -> None:
    engine = OpsEngine(settings)
    app = create_app(settings, engine)
    routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}
    expected = {
        ("GET", "/v1/capabilities"),
        ("POST", "/v1/tasks/analyze"),
        ("POST", "/v1/tasks/run"),
        ("POST", "/v1/tasks/probe"),
        ("POST", "/v1/knowledge"),
        ("GET", "/v1/tasks"),
        ("GET", "/v1/tasks/{task_id}"),
        ("GET", "/v1/tasks/{task_id}/events"),
        ("GET", "/v1/tasks/{task_id}/knowledge"),
        ("POST", "/v1/tasks/{task_id}/cancel"),
        ("POST", "/v1/actions/{action_id}/decision"),
        ("GET", "/v1/events"),
        ("GET", "/v1/knowledge"),
        ("POST", "/v1/knowledge/search"),
        ("GET", "/v1/knowledge/stats"),
    }
    assert expected.issubset(routes)
    with TestClient(app) as client:
        resp = client.get("/healthz").json()
        assert resp["ok"] is True
        assert resp["profile"] == "test"
        assert resp["agent"] == "Local Error Analysis Agent"
    engine.store.close()


def test_analysis_request_contract() -> None:
    """Verify the AnalysisRequest model validates correctly."""
    req = AnalysisRequest(text="ModuleNotFoundError: No module named 'click'")
    assert req.text == "ModuleNotFoundError: No module named 'click'"
    assert req.source == "stdin"
    assert req.language == ""

    # Empty text should be allowed (validated at API level)
    req2 = AnalysisRequest(text="")
    assert req2.text == ""

    # With full context
    req3 = AnalysisRequest(
        text="ERROR: connection refused",
        source="terminal",
        language="python",
        command="pytest",
        cwd="/home/user/project",
        exit_code=1,
    )
    assert req3.exit_code == 1
    assert req3.language == "python"


def test_task_kind_enum() -> None:
    assert TaskKind.ANALYZE.value == "analyze"
    assert TaskKind.VERIFY.value == "verify"
    assert TaskKind.PROBE.value == "probe"
    assert TaskKind.KNOWLEDGE_RECORD.value == "knowledge_record"
