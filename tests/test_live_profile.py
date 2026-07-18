from __future__ import annotations

import os
import stat
from dataclasses import replace

import pytest

from aiops_agent.providers import DockerProvider, NvidiaProvider


def test_live_providers_never_report_fixture_sources(settings) -> None:
    live = replace(settings, profile="live")
    docker = DockerProvider(live).capabilities()
    nvidia = NvidiaProvider("live").status()
    assert "demo" not in str(docker.get("source", "")).lower()
    assert "fixture" not in str(docker).lower()
    assert "demo" not in str(nvidia.get("source", "")).lower()
    assert "fixture" not in str(nvidia).lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not enforced on Windows")
def test_operator_parent_directories_are_traversable_but_not_listable(settings) -> None:
    settings.ensure_directories()
    assert stat.S_IMODE(settings.state_dir.stat().st_mode) == 0o711
    assert stat.S_IMODE(settings.run_dir.stat().st_mode) == 0o711
    assert stat.S_IMODE(settings.backup_dir.stat().st_mode) == 0o750
