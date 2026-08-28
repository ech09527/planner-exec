"""MCP server tool tier configuration."""

from __future__ import annotations

import os


def env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def debug_tools_enabled() -> bool:
    return env_flag("PE_MCP_DEBUG_TOOLS")


def observe_tools_enabled() -> bool:
    return env_flag("PE_MCP_OBSERVE_TOOLS")
