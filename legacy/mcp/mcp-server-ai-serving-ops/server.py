from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from diagnostics import (
    apply_serving_config_patch as build_apply_serving_config_patch,
    as_json,
    backup_config as build_backup_config,
    detect_serving_framework as build_detect_serving_framework,
    inspect_service_health as build_inspect_service_health,
    parse_serving_log as build_parse_serving_log,
    rollback_config as build_rollback_config,
    suggest_serving_config_patch as build_suggest_serving_config_patch,
    validate_model_path as build_validate_model_path,
    verify_service_recovery as build_verify_service_recovery,
    verify_serving_config as build_verify_serving_config,
)


mcp = FastMCP("mcp-server-ai-serving-ops")


@mcp.tool()
async def inspect_service_health(url: str, timeout_seconds: float = 3.0) -> str:
    """Inspect an AI serving health or OpenAI-compatible endpoint."""
    return as_json(build_inspect_service_health(url, timeout_seconds))


@mcp.tool()
async def detect_serving_framework(url: str = "", log_path: str = "") -> str:
    """Detect serving framework from endpoint hints and logs."""
    return as_json(build_detect_serving_framework(url, log_path))


@mcp.tool()
async def validate_model_path(model_path: str) -> str:
    """Check whether a configured model path exists."""
    return as_json(build_validate_model_path(model_path))


@mcp.tool()
async def parse_serving_log(log_path: str = "", framework: str = "generic") -> str:
    """Parse AI serving logs for 503, model load timeout, OOM, model path, and port errors."""
    return as_json(build_parse_serving_log(log_path, framework))


@mcp.tool()
async def verify_serving_config(config_path: str = "", framework: str = "generic") -> str:
    """Validate an AI serving JSON config and highlight risky settings."""
    return as_json(build_verify_serving_config(config_path, framework))


@mcp.tool()
async def suggest_serving_config_patch(config_path: str = "", symptom: str = "", framework: str = "generic") -> str:
    """Suggest a safe AI serving config patch for model-load or memory-pressure symptoms."""
    return as_json(build_suggest_serving_config_patch(config_path, symptom, framework))


@mcp.tool()
async def backup_config(config_path: str = "") -> str:
    """Create a timestamped backup of an AI serving config."""
    return as_json(build_backup_config(config_path))


@mcp.tool()
async def apply_serving_config_patch(config_path: str = "", patch_json: str = "", dry_run: bool = True) -> str:
    """Apply or preview a restricted AI serving config patch."""
    return as_json(build_apply_serving_config_patch(config_path, patch_json, dry_run))


@mcp.tool()
async def rollback_config(config_path: str = "", backup_path: str = "", dry_run: bool = True) -> str:
    """Restore an AI serving config from a backup."""
    return as_json(build_rollback_config(config_path, backup_path, dry_run))


@mcp.tool()
async def verify_service_recovery(url: str, expected_status: int = 200, timeout_seconds: float = 3.0) -> str:
    """Verify that an AI serving endpoint has recovered."""
    return as_json(build_verify_service_recovery(url, expected_status, timeout_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-ai-serving-ops")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    _ = parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
