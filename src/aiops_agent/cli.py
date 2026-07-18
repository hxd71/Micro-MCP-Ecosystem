from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import click
import httpx

from .config import Settings

TERMINAL_STATUSES = {"succeeded", "failed", "rolled_back", "cancelled"}


class AgentClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = (
            settings.operator_token_path.read_text(encoding="utf-8").strip()
            if settings.operator_token_path.exists()
            else ""
        )
        self.client: httpx.Client
        if os.name != "nt" and settings.socket_path.exists():
            transport = httpx.HTTPTransport(uds=str(settings.socket_path))
            self.client = httpx.Client(transport=transport, base_url="http://aiops.local", timeout=60)
        else:
            self.client = httpx.Client(base_url=f"http://{settings.web_host}:{settings.web_port}", timeout=60)

    def request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.request(
                method, path, json=json_body, headers={"X-AIOPS-Token": self.token} if self.token else {}
            )
        except httpx.HTTPError as exc:
            raise click.ClickException(f"cannot reach aiops-agent: {exc}") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise click.ClickException(f"agent returned HTTP {response.status_code}: {detail}")
        return response.json()


def print_json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--profile", type=click.Choice(["live", "test", "demo"]), default=None)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None, profile: str | None) -> None:
    """Operate the local Docker/NVIDIA/vLLM Agent."""
    settings = Settings.load(config_path, profile=profile)  # type: ignore[arg-type]
    ctx.obj = AgentClient(settings)


@cli.command()
@click.pass_obj
def doctor(client: AgentClient) -> None:
    """Show live capability checks."""
    print_json(client.request("GET", "/v1/capabilities"))


@cli.command()
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def deploy(client: AgentClient, manifest: Path) -> None:
    """Validate a manifest and create an approval-gated deployment task."""
    print_json(client.request("POST", "/v1/tasks/deploy", {"manifest": manifest.read_text(encoding="utf-8")}))


@cli.command()
@click.argument("service")
@click.option("--symptom", default="", help="Optional operator-observed symptom.")
@click.pass_obj
def diagnose(client: AgentClient, service: str, symptom: str) -> None:
    """Create a read-only diagnosis task."""
    print_json(client.request("POST", "/v1/tasks/diagnose", {"service": service, "symptom": symptom}))


@cli.group()
def security() -> None:
    """Container and image security operations."""


@security.command("scan")
@click.argument("service")
@click.pass_obj
def security_scan(client: AgentClient, service: str) -> None:
    print_json(client.request("POST", "/v1/tasks/security", {"service": service}))


@cli.group()
def task() -> None:
    """Inspect Agent tasks."""


@task.command("list")
@click.option("--limit", default=50, type=click.IntRange(1, 500))
@click.pass_obj
def task_list(client: AgentClient, limit: int) -> None:
    print_json(client.request("GET", f"/v1/tasks?limit={limit}"))


@task.command("show")
@click.argument("task_id")
@click.pass_obj
def task_show(client: AgentClient, task_id: str) -> None:
    print_json(client.request("GET", f"/v1/tasks/{task_id}"))


@task.command("watch")
@click.argument("task_id")
@click.option("--interval", default=1.0, type=click.FloatRange(0.2, 10.0))
@click.pass_obj
def task_watch(client: AgentClient, task_id: str, interval: float) -> None:
    last_status = ""
    while True:
        detail = client.request("GET", f"/v1/tasks/{task_id}")
        status = detail["task"]["status"]
        if status != last_status:
            click.echo(f"{detail['task']['updated_at']}  {status}")
            last_status = status
        if status in TERMINAL_STATUSES or status == "waiting_approval":
            print_json(detail)
            return
        time.sleep(interval)


@cli.group()
def action() -> None:
    """Approve or reject immutable action proposals."""


def decide(client: AgentClient, action_id: str, decision: str, yes: bool) -> None:
    proposal = client.request("GET", f"/v1/actions/{action_id}")
    click.echo(f"Action: {proposal['kind']} -> {proposal['target']}")
    click.echo(f"Risk: {proposal['risk']}")
    click.echo(f"Digest: {proposal['digest']}")
    for step in proposal["steps"]:
        click.echo(f"  {step['order']}. {step['description']}")
    if not yes and not click.confirm(f"{decision.title()} this exact action proposal?"):
        raise click.Abort()
    print_json(
        client.request(
            "POST",
            f"/v1/actions/{action_id}/decision",
            {"decision": decision, "action_digest": proposal["digest"]},
        )
    )


@action.command("approve")
@click.argument("action_id")
@click.option("--yes", is_flag=True, help="Skip the local confirmation prompt.")
@click.pass_obj
def action_approve(client: AgentClient, action_id: str, yes: bool) -> None:
    decide(client, action_id, "approve", yes)


@action.command("reject")
@click.argument("action_id")
@click.option("--yes", is_flag=True, help="Skip the local confirmation prompt.")
@click.pass_obj
def action_reject(client: AgentClient, action_id: str, yes: bool) -> None:
    decide(client, action_id, "reject", yes)


@cli.command()
@click.argument("revision_id")
@click.pass_obj
def rollback(client: AgentClient, revision_id: str) -> None:
    """Create an approval-gated rollback task."""
    print_json(client.request("POST", "/v1/tasks/rollback", {"revision_id": revision_id}))


@cli.group()
def web() -> None:
    """Manage the local Web UI session."""


@web.command("login")
@click.pass_obj
def web_login(client: AgentClient) -> None:
    result = client.request("POST", "/v1/web/login-code")
    click.echo(result["url"])
    click.echo("This one-time local link expires in 120 seconds.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
