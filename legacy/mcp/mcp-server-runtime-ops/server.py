from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from diagnostics import (
    as_json,
    check_disk_usage as build_check_disk_usage,
    check_env_vars as build_check_env_vars,
    check_memory_usage as build_check_memory_usage,
    check_port as build_check_port,
    check_process as build_check_process,
    detect_recent_errors as build_detect_recent_errors,
    grep_log as build_grep_log,
    list_listening_ports as build_list_listening_ports,
    tail_log as build_tail_log,
)


mcp = FastMCP("mcp-server-runtime-ops")


@mcp.tool()
async def check_process(keyword: str) -> str:
    """Find local processes whose command line contains a keyword."""
    return as_json(build_check_process(keyword))


@mcp.tool()
async def check_port(port: int) -> str:
    """Check whether a TCP port is listening or connectable on localhost."""
    return as_json(build_check_port(port))


@mcp.tool()
async def list_listening_ports() -> str:
    """List listening TCP ports using local OS tools."""
    return as_json(build_list_listening_ports())


@mcp.tool()
async def check_disk_usage(path: str = ".") -> str:
    """Check disk usage for a path."""
    return as_json(build_check_disk_usage(path))


@mcp.tool()
async def check_memory_usage() -> str:
    """Check host memory usage."""
    return as_json(build_check_memory_usage())


@mcp.tool()
async def check_env_vars(names: list[str] | str) -> str:
    """Check whether environment variables are present."""
    return as_json(build_check_env_vars(names))


@mcp.tool()
async def tail_log(path: str, lines: int = 100) -> str:
    """Return the last N lines of a log file."""
    return as_json(build_tail_log(path, lines))


@mcp.tool()
async def grep_log(path: str, pattern: str, max_matches: int = 20) -> str:
    """Search a log file with a regex pattern."""
    return as_json(build_grep_log(path, pattern, max_matches))


@mcp.tool()
async def detect_recent_errors(log_path: str) -> str:
    """Find recent error-like lines in a log file."""
    return as_json(build_detect_recent_errors(log_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-runtime-ops")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    _ = parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
