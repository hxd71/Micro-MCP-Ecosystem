from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from diagnostics import (
    as_json,
    check_accelerator_env as build_check_accelerator_env,
    check_accelerator_status as build_check_accelerator_status,
    detect_memory_pressure as build_detect_memory_pressure,
    list_accelerator_processes as build_list_accelerator_processes,
)


mcp = FastMCP("mcp-server-accelerator-ops")


@mcp.tool()
async def check_accelerator_status(provider: str = "auto") -> str:
    """Check GPU/NPU status using nvidia-smi, npu-smi, or mock fixtures."""
    return as_json(build_check_accelerator_status(provider))


@mcp.tool()
async def check_accelerator_env(provider: str = "auto") -> str:
    """Check CUDA/CANN related environment and CLI availability."""
    return as_json(build_check_accelerator_env(provider))


@mcp.tool()
async def detect_memory_pressure(provider: str = "auto") -> str:
    """Detect accelerator memory pressure."""
    return as_json(build_detect_memory_pressure(provider))


@mcp.tool()
async def list_accelerator_processes(provider: str = "auto") -> str:
    """List GPU/NPU processes and memory usage."""
    return as_json(build_list_accelerator_processes(provider))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-accelerator-ops")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    _ = parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
