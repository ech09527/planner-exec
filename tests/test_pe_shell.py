"""Tests for pe_shell guard (mechanical rules only, no LLM)."""

from planner_exec.pe_shell import guard_command


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
