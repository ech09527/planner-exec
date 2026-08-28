"""Planner-exec data directory resolution."""

from __future__ import annotations

import os
from pathlib import Path

# Historical default path (typo preserved for backward compatibility).
_DEFAULT_ROOT = Path.home() / ".planer-exec"


def planner_data_root() -> Path:
    """Return data root. Override with PE_DATA_DIR (e.g. ~/.planner-exec)."""
    raw = os.environ.get("PE_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_ROOT


def legacy_tasks_dir() -> Path:
    return planner_data_root() / "tasks"
