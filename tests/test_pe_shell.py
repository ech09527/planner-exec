"""Tests for pe_shell guard (mechanical rules only, no LLM)."""

from pathlib import Path

from planner_exec.pe_actions import apply_actions
from planner_exec.pe_shell import guard_command, run_guarded_shell


def test_blocklist_rm_rf():
    r = guard_command("rm -rf /tmp/x", mode="off")
    assert not r.allowed
    assert r.source == "blocklist"


def test_allowlist_pytest():
    r = guard_command("pytest -q tests/", mode="ai_gate", use_ai=False)
    assert r.allowed
    assert r.source == "allowlist"


def test_strict_rejects_unknown():
    r = guard_command("custom-tool --magic", mode="strict", use_ai=False)
    assert not r.allowed
    assert r.source == "mode"


def test_off_allows_safe_unknown():
    r = guard_command("custom-tool --magic", mode="off", use_ai=False)
    assert r.allowed


def test_run_guarded_shell_timeout_returns_error(tmp_path: Path):
    r = run_guarded_shell(str(tmp_path), "sleep 5", timeout=1, shell_mode="off")
    assert r["ok"] is False
    assert r.get("timed_out") is True
    assert "timed out" in (r.get("error") or "")


def test_run_guarded_shell_invalid_timeout(tmp_path: Path):
    r = run_guarded_shell(str(tmp_path), "echo hi", timeout="nope", shell_mode="off")  # type: ignore[arg-type]
    assert r["ok"] is False
    assert "invalid timeout" in (r.get("error") or "")


def test_apply_actions_bad_timeout_no_raise(tmp_path: Path):
    out = apply_actions(
        [{"type": "run_shell", "command": "echo hi", "timeout": "nope"}],
        str(tmp_path),
    )
    assert len(out) == 1
    assert out[0]["ok"] is False
    assert "invalid timeout" in (out[0].get("error") or "")
