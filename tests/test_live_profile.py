from __future__ import annotations

from dataclasses import replace

from aiops_agent.providers import DockerProvider, NvidiaProvider


def test_live_providers_never_report_fixture_sources(settings) -> None:
    live = replace(settings, profile="live")
    docker = DockerProvider(live).capabilities()
    nvidia = NvidiaProvider("live").status()
    assert "demo" not in str(docker.get("source", "")).lower()
    assert "fixture" not in str(docker).lower()
    assert "demo" not in str(nvidia.get("source", "")).lower()
    assert "fixture" not in str(nvidia).lower()
