from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from termops.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.load(profile="test"),
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        run_dir=tmp_path / "run",
        approval_ttl_seconds=900,
    )
