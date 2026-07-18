from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from diagnostics import (
    apply_config_patch as build_apply_config_patch,
    apply_mindie_config_patch as build_apply_mindie_config_patch,
    as_json,
    backup_config as build_backup_config,
    backup_config_file,
    check_cann_environment,
    check_npu_status,
    diagnose_inference_issue,
    inspect_inference_endpoint,
    parse_mindie_log_file,
    remediation_plan_markdown,
    restart_service_with_approval as build_restart_service_with_approval,
    restart_service_dry_run,
    rollback_config as build_rollback_config,
    suggest_config_patch as build_suggest_config_patch,
    suggest_mindie_config_patch as build_suggest_mindie_config_patch,
    verify_service_recovery as build_verify_service_recovery,
    verify_mindie_config_file,
)

mcp = FastMCP("mcp-server-ascend-ops")


@mcp.tool()
async def check_cann_env() -> str:
    """Check common CANN/Ascend environment variables and npu-smi availability."""
    return as_json(check_cann_environment())


@mcp.tool()
async def check_npu_status_info(use_mock_when_unavailable: bool = True) -> str:
    """Run npu-smi info when available, otherwise return a bundled demo sample.

    Args:
        use_mock_when_unavailable: Use bundled sample output when npu-smi is not installed.
    """
    return as_json(check_npu_status(use_mock_when_unavailable))


@mcp.tool()
async def parse_mindie_log(log_path: str = "") -> str:
    """Parse MindIE/CANN style logs and extract evidence for common inference failures.

    Args:
        log_path: Absolute or relative log path. Empty value uses the bundled demo log.
    """
    return as_json(parse_mindie_log_file(log_path or None))


@mcp.tool()
async def inspect_inference_health(url: str, timeout_seconds: float = 3.0) -> str:
    """Inspect an inference service HTTP health or completion endpoint.

    Args:
        url: HTTP endpoint to inspect.
        timeout_seconds: Request timeout in seconds.
    """
    return as_json(inspect_inference_endpoint(url, timeout_seconds))


@mcp.tool()
async def diagnose_ascend_inference_issue(symptom: str, log_path: str = "") -> str:
    """Diagnose an Ascend inference issue from symptom text, logs, env and NPU status.

    Args:
        symptom: User-observed issue, such as 503, model load timeout, or CANN init failure.
        log_path: Optional MindIE/CANN log path. Empty value uses the bundled demo log.
    """
    return as_json(diagnose_inference_issue(symptom, log_path or None))


@mcp.tool()
async def generate_ascend_remediation_plan(symptom: str, log_path: str = "") -> str:
    """Generate a repair-oriented plan with evidence, safe checks and approval-required actions.

    Args:
        symptom: User-observed issue.
        log_path: Optional MindIE/CANN log path. Empty value uses the bundled demo log.
    """
    return remediation_plan_markdown(symptom, log_path or None)


@mcp.tool()
async def verify_mindie_config(config_path: str = "") -> str:
    """Validate a MindIE-style JSON config and highlight risky inference settings.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
    """
    return as_json(verify_mindie_config_file(config_path or None))


@mcp.tool()
async def suggest_mindie_config_patch(config_path: str = "", symptom: str = "") -> str:
    """Suggest a safe config patch for memory pressure or model-load-timeout symptoms.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
        symptom: User-observed issue.
    """
    return as_json(build_suggest_mindie_config_patch(config_path or None, symptom))


@mcp.tool()
async def suggest_config_patch(config_path: str = "", symptom: str = "") -> str:
    """Suggest a safe generic inference config patch.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
        symptom: User-observed issue.
    """
    return as_json(build_suggest_config_patch(config_path or None, symptom))


@mcp.tool()
async def backup_mindie_config(config_path: str = "") -> str:
    """Create a timestamped backup of a config file before mutation.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
    """
    return as_json(backup_config_file(config_path or None))


@mcp.tool()
async def backup_config(config_path: str = "") -> str:
    """Create a timestamped backup of a config file before mutation.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
    """
    return as_json(build_backup_config(config_path or None))


@mcp.tool()
async def apply_mindie_config_patch(
    config_path: str = "",
    patch_json: str = "",
    dry_run: bool = True,
) -> str:
    """Apply or preview a restricted MindIE config patch.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
        patch_json: JSON object containing allowed config keys.
        dry_run: When true, preview changes without writing.
    """
    return as_json(build_apply_mindie_config_patch(config_path or None, patch_json, dry_run))


@mcp.tool()
async def apply_config_patch(
    config_path: str = "",
    patch_json: str = "",
    dry_run: bool = True,
) -> str:
    """Apply or preview a restricted inference config patch.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
        patch_json: JSON object containing allowed config keys.
        dry_run: When true, preview changes without writing.
    """
    return as_json(build_apply_config_patch(config_path or None, patch_json, dry_run))


@mcp.tool()
async def rollback_config(config_path: str = "", backup_path: str = "", dry_run: bool = True) -> str:
    """Restore a config file from a Hub-created backup.

    Args:
        config_path: Config path. Empty value uses the bundled demo config.
        backup_path: Backup file path created by backup_config/apply_config_patch.
        dry_run: When true, preview rollback without writing.
    """
    return as_json(build_rollback_config(config_path or None, backup_path, dry_run))


@mcp.tool()
async def restart_service_plan(service_name: str, restart_command: str = "") -> str:
    """Build a high-risk service restart plan without executing it.

    Args:
        service_name: Service name to restart.
        restart_command: Optional explicit restart command.
    """
    return as_json(restart_service_dry_run(service_name, restart_command))


@mcp.tool()
async def restart_service_with_approval(
    service_name: str,
    restart_command: str = "",
    execute: bool = False,
    confirm_text: str = "",
) -> str:
    """Restart a service only after Hub approval and server-side confirmation checks.

    Args:
        service_name: Service name to restart.
        restart_command: Optional explicit restart command.
        execute: When false, return a dry-run plan. True still requires Hub approval and env opt-in.
        confirm_text: Must exactly match service_name when execute=true.
    """
    return as_json(build_restart_service_with_approval(service_name, restart_command, execute, confirm_text))


@mcp.tool()
async def verify_service_recovery(url: str, expected_status: int = 200, timeout_seconds: float = 3.0) -> str:
    """Verify that an inference service has recovered after a repair.

    Args:
        url: Health endpoint or completion endpoint to inspect.
        expected_status: Expected HTTP status, usually 200.
        timeout_seconds: Request timeout in seconds.
    """
    return as_json(build_verify_service_recovery(url, expected_status, timeout_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-ascend-ops")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport mode. This server is intended for stdio in mcp-core-hub.",
    )
    _ = parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
