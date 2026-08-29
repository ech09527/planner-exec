"""Tests for MCP _run_pe error surfacing (SystemExit → ToolError)."""

from __future__ import annotations

import argparse

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from planner_exec.server import _run_pe


def test_run_pe_converts_systemexit_to_toolerror():
    def boom(_args):
        raise SystemExit("ERROR [400]: plan.goal_confirmed is required")

    with pytest.raises(ToolError, match="goal_confirmed"):
        _run_pe(boom, argparse.Namespace())


def test_run_pe_systemexit_zero_ok():
    def ok(_args):
        print('{"ok": true}')
        raise SystemExit(0)

    out = _run_pe(ok, argparse.Namespace())
    assert '"ok": true' in out


def test_run_pe_success_returns_stdout():
    def ok(_args):
        print('{"status": "planned"}')

    assert _run_pe(ok, argparse.Namespace()) == '{"status": "planned"}'
