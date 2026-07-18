from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from diagnostics import (
    as_json,
    check_container_env as build_check_container_env,
    check_container_health as build_check_container_health,
    check_container_mounts as build_check_container_mounts,
    check_container_ports as build_check_container_ports,
    get_container_logs as build_get_container_logs,
    inspect_container as build_inspect_container,
    list_containers as build_list_containers,
    restart_container_dry_run as build_restart_container_dry_run,
    restart_container_with_approval as build_restart_container_with_approval,
)


mcp = FastMCP("mcp-server-container-ops")


@mcp.tool()
async def list_containers() -> str:
    """List Docker containers or return demo fixtures when Docker is unavailable."""
    return as_json(build_list_containers())


@mcp.tool()
async def inspect_container(container: str) -> str:
    """Inspect a Docker container."""
    return as_json(build_inspect_container(container))


@mcp.tool()
async def get_container_logs(container: str, lines: int = 100) -> str:
    """Get recent container logs."""
    return as_json(build_get_container_logs(container, lines))


@mcp.tool()
async def check_container_health(container: str) -> str:
    """Check container running and health status."""
    return as_json(build_check_container_health(container))


@mcp.tool()
async def check_container_ports(container: str) -> str:
    """Check container port mappings."""
    return as_json(build_check_container_ports(container))


@mcp.tool()
async def check_container_mounts(container: str) -> str:
    """Check container mounts and model volume visibility hints."""
    return as_json(build_check_container_mounts(container))


@mcp.tool()
async def check_container_env(container: str, names: list[str] | str | None = None) -> str:
    """Check container environment variables."""
    return as_json(build_check_container_env(container, names))


@mcp.tool()
async def restart_container_dry_run(container: str) -> str:
    """Build a high-risk container restart plan without executing it."""
    return as_json(build_restart_container_dry_run(container))


@mcp.tool()
async def restart_container_with_approval(container: str, execute: bool = False, confirm_text: str = "") -> str:
    """Restart a container only after Hub approval and server-side opt-in."""
    return as_json(build_restart_container_with_approval(container, execute, confirm_text))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-container-ops")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    _ = parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
