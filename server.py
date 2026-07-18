"""Deprecated, read-only MCP compatibility entrypoint.

The production Local AI Ops Agent is started with ``aiops-agent``. This module
intentionally exposes no shell execution, file reading, HTTP listener, or
mutation tools.
"""

from __future__ import annotations

import argparse
import json

from mcp.server.fastmcp import FastMCP

from aiops_agent.config import Settings
from aiops_agent.models import InferenceServiceManifest
from aiops_agent.providers import NvidiaProvider, build_docker_provider
from aiops_agent.security import validate_manifest_policy

mcp = FastMCP("local-ai-ops-agent-compat")


@mcp.tool()
async def get_aiops_capabilities(profile: str = "live") -> str:
    """Return read-only Docker and NVIDIA capability checks."""
    settings = Settings.load(profile=profile)  # type: ignore[arg-type]
    docker = build_docker_provider(settings)
    result = {
        "deprecated_compatibility_server": True,
        "profile": settings.profile,
        "docker": docker.capabilities(),
        "nvidia": NvidiaProvider(settings.profile).status(),
        "production_entrypoint": "aiops-agent",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def validate_inference_service_manifest(manifest_yaml: str, profile: str = "live") -> str:
    """Validate a vLLM InferenceService manifest without changing host state."""
    try:
        settings = Settings.load(profile=profile)  # type: ignore[arg-type]
        manifest = InferenceServiceManifest.from_yaml(manifest_yaml)
        errors = validate_manifest_policy(manifest, settings)
        result = {
            "ok": not errors,
            "service": manifest.metadata.name,
            "errors": errors,
            "mutation_performed": False,
        }
    except Exception as exc:
        result = {"ok": False, "errors": [str(exc)], "mutation_performed": False}
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deprecated read-only MCP compatibility server")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
