"""CLI integration tests covering stdin, file input, command wrapping, and structured JSON output."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from termops.api import create_app
from termops.cli import AgentClient, cli
from termops.engine import OpsEngine
from termops.models import TaskStatus


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _start_event_loop() -> asyncio.AbstractEventLoop:
    """Start an asyncio event loop in a background thread so spawned tasks run."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop


def _make_client(engine: OpsEngine) -> TestClient:
    """Create a FastAPI TestClient for the engine, then wrap it inside a mock AgentClient."""
    app = create_app(engine.settings, engine)
    return TestClient(app)


def _auth_header(engine: OpsEngine) -> dict[str, str]:
    return {"X-Operator-Token": engine.operator_token}


def _mock_request(engine: OpsEngine, tc: TestClient, method: str, path: str, json_body: dict | None = None) -> object:
    kwargs = {"headers": _auth_header(engine)}
    if json_body is not None:
        kwargs["json"] = json_body
    resp = tc.request(method, path, **kwargs)
    if resp.is_error:
        from click import ClickException
        detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        raise ClickException(f"agent returned HTTP {resp.status_code}: {detail}")
    return resp.json()


def _submit_analysis(client: TestClient, engine: OpsEngine, text: str, **kwargs) -> dict:
    resp = client.post(
        "/v1/tasks/analyze",
        headers=_auth_header(engine),
        json={"text": text, "source": "stderr", "language": "python", **kwargs},
    )
    assert resp.status_code == 202
    return resp.json()


def _wait_for_status(client: TestClient, engine: OpsEngine, task_id: str, expected: set[str]) -> str:
    """Poll until the task reaches an expected status."""
    for _ in range(200):
        detail = client.get(f"/v1/tasks/{task_id}", headers=_auth_header(engine)).json()
        status = detail["task"]["status"]
        if status in expected:
            return status
        time.sleep(0.02)
    # Show actual status for debugging
    detail = client.get(f"/v1/tasks/{task_id}", headers=_auth_header(engine)).json()
    actual = detail["task"]["status"]
    error = detail["task"].get("error", "")
    raise AssertionError(f"Task {task_id} did not reach {expected}; actual status={actual!r} error={error!r}")

def _process_task_sync(engine: OpsEngine, task_id: str) -> None:
    """Run the task processing coroutine synchronously."""
    asyncio.run(engine.process_task(task_id))


class TestAnalyzeCommand:
    """Test the `erra analyze` command via --text, --file, and stdin."""

    def test_analyze_with_text(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "analyze", "--text", "ModuleNotFoundError: No module named 'click'"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kind"] == "analyze"
        assert data["status"] in {"queued", "succeeded", "waiting_approval", "running"}
        engine.store.close()

    def test_analyze_with_file(self, runner, settings, tmp_path: Path):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        log_file = tmp_path / "error.log"
        log_file.write_text("ModuleNotFoundError: No module named 'click'\n", encoding="utf-8")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "analyze", "--file", str(log_file)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kind"] == "analyze"
        engine.store.close()

    def test_analyze_from_stdin(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "analyze"],
                input="ModuleNotFoundError: No module named 'click'\n",
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kind"] == "analyze"
        engine.store.close()

    def test_analyze_with_context(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                [
                    "--json", "analyze",
                    "--text", "ModuleNotFoundError: No module named 'click'",
                    "--language", "python",
                    "--command", "pytest tests/",
                    "--cwd", "/home/user/project",
                    "--exit-code", "1",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kind"] == "analyze"
        engine.store.close()

    def test_analyze_no_text_is_error(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["analyze", "--text", ""], input="", catch_exceptions=False)
        assert result.exit_code != 0
        assert "no analysis text" in result.output.lower()
        engine.store.close()

    def test_analyze_formatted_output(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["analyze", "--text", "ModuleNotFoundError: No module named 'click'"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "Task:" in result.output
        assert "Status:" in result.output
        engine.store.close()


class TestRunCommand:
    """Test the `erra run` command which wraps a local command execution."""

    def test_run_with_python_command(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "run", sys.executable, "-c", "print('hello')"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kind"] == "analyze"
        assert data["status"] in {"queued", "succeeded", "running"}
        engine.store.close()

    def test_run_no_args_is_error(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["run"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "requires a command" in result.output.lower()
        engine.store.close()

    def test_run_formatted_output(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["run", sys.executable, "-c", "print('hello')"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "Task:" in result.output
        engine.store.close()


class TestDoctorCommand:
    """Test the `erra doctor` command."""

    def test_doctor_json(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "doctor"], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["profile"] is not None
        assert "llm" in data
        engine.store.close()

    def test_doctor_formatted(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["doctor"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Profile" in result.output
        assert "MAPE-K" in result.output
        engine.store.close()


class TestTaskCommands:
    """Test the `erra task` subcommands."""

    def test_task_list(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        _submit_analysis(tc, engine, "ModuleNotFoundError: No module named 'click'\n")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "task", "list", "--limit", "5"], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1
        engine.store.close()

    def test_task_list_formatted(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        _submit_analysis(tc, engine, "ModuleNotFoundError: No module named 'click'\n")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["task", "list"], catch_exceptions=False)
        assert result.exit_code == 0
        engine.store.close()

    def test_task_show(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = _submit_analysis(tc, engine, "ModuleNotFoundError: No module named 'click'\n")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "task", "show", task["id"]], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task"]["id"] == task["id"]
        engine.store.close()

    def test_task_show_formatted(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = _submit_analysis(tc, engine, "ModuleNotFoundError: No module named 'click'\n")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["task", "show", task["id"]], catch_exceptions=False)
        assert result.exit_code == 0
        assert task["id"][:12] in result.output
        engine.store.close()

    def test_task_cancel(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = _submit_analysis(tc, engine, "ModuleNotFoundError: No module named 'click'\n")
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "task", "cancel", task["id"]], catch_exceptions=False)
        assert result.exit_code == 0
        engine.store.close()

    def test_task_watch(self, runner, settings):
        from termops.store import TaskKind as TK
        from termops.models import TargetRef
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = engine.store.create_task(
            TK.ANALYZE,
            TargetRef(kind="terminal", name="test"),
            {"text": "ModuleNotFoundError: No module named 'click'", "source": "stderr", "language": "python", "command": "", "cwd": "", "exit_code": None, "files": [], "history_task_ids": []},
        )
        task_id = task.id
        asyncio.run(engine.process_task(task_id))
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "task", "watch", task_id, "--interval", "0.2"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        engine.store.close()


class TestActionCommands:
    """Test the `erra action approve/reject` commands."""

    def test_action_approve(self, runner, settings):
        from termops.models import ApprovalDecision
        from termops.store import TaskKind as TK
        from termops.models import TargetRef
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = engine.store.create_task(
            TK.ANALYZE,
            TargetRef(kind="terminal", name="test"),
            {"text": "ModuleNotFoundError: No module named 'requests'", "source": "stderr", "language": "python", "command": f'"{sys.executable}" -c "print(1)"', "cwd": "", "exit_code": 1, "files": [], "history_task_ids": []},
        )
        task_id = task.id
        asyncio.run(engine.process_task(task_id))
        detail = tc.get(f"/v1/tasks/{task_id}", headers=_auth_header(engine)).json()
        if detail["task"]["status"] != TaskStatus.WAITING_APPROVAL.value:
            pytest.skip(f"task status is {detail['task']['status']}, not waiting_approval")
        action = detail["actions"][0]
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "action", "approve", "--yes", action["id"]],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Output contains two JSON objects (proposal + decision); check for expected status
        assert '"status": "executing"' in result.output
        engine.store.close()

    def test_action_reject(self, runner, settings):
        from termops.store import TaskKind as TK
        from termops.models import TargetRef
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        task = engine.store.create_task(
            TK.ANALYZE,
            TargetRef(kind="terminal", name="test"),
            {"text": "ModuleNotFoundError: No module named 'requests'", "source": "stderr", "language": "python", "command": f'"{sys.executable}" -c "print(1)"', "cwd": "", "exit_code": 1, "files": [], "history_task_ids": []},
        )
        task_id = task.id
        asyncio.run(engine.process_task(task_id))
        detail = tc.get(f"/v1/tasks/{task_id}", headers=_auth_header(engine)).json()
        if detail["task"]["status"] != TaskStatus.WAITING_APPROVAL.value:
            pytest.skip(f"task status is {detail['task']['status']}, not waiting_approval")
        action = detail["actions"][0]
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "action", "reject", "--yes", action["id"]],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Output contains two JSON objects (proposal + decision); check for expected status
        assert '"status": "rejected"' in result.output
        engine.store.close()


class TestWebCommand:
    """Test the `erra web login` command."""

    def test_web_login(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["web", "login"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "code=" in result.output.lower()
        engine.store.close()


class TestJsonFlag:
    """Verify --json flag produces valid JSON across all commands."""

    def test_doctor_json_is_valid(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "doctor"], catch_exceptions=False)
        json.loads(result.output)
        engine.store.close()

    def test_analyze_json_is_valid(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "analyze", "--text", "ModuleNotFoundError: No module named 'click'"],
                catch_exceptions=False,
            )
        json.loads(result.output)
        engine.store.close()

    def test_run_json_is_valid(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(
                cli,
                ["--json", "run", sys.executable, "-c", "print('hello')"],
                catch_exceptions=False,
            )
        json.loads(result.output)
        engine.store.close()

    def test_task_list_json_is_valid(self, runner, settings):
        engine = OpsEngine(settings)
        tc = _make_client(engine)
        with patch.object(AgentClient, "request", side_effect=lambda *a,**kw: _mock_request(engine, tc, *a, **kw)):
            result = runner.invoke(cli, ["--json", "task", "list"], catch_exceptions=False)
        json.loads(result.output)
        engine.store.close()