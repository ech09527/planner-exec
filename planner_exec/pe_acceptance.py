"""Mechanical acceptance checks after node execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pe_shell import run_guarded_shell


def _check_file_exists(workspace: str | None, spec: dict[str, Any]) -> dict[str, Any]:
    rel = spec.get("path", "")
    if not workspace:
        return {"type": "file_exists", "path": rel, "passed": False, "error": "no workspace"}
    root = Path(workspace).resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)):
        return {"type": "file_exists", "path": rel, "passed": False, "error": "path escapes workspace"}
    ok = target.is_file()
    return {"type": "file_exists", "path": rel, "passed": ok, "error": None if ok else "file not found"}


def _check_shell(workspace: str | None, spec: dict[str, Any]) -> dict[str, Any]:
    cmd = spec.get("command", "")
    expect = int(spec.get("expect_exit", 0))
    raw_timeout = spec.get("timeout", 120)
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        return {
            "type": "shell",
            "command": cmd,
            "passed": False,
            "error": f"invalid timeout value: {raw_timeout!r}",
        }
    if not cmd:
        return {"type": "shell", "command": cmd, "passed": False, "error": "empty command"}

    rec = run_guarded_shell(workspace, cmd, timeout=timeout)
    if not rec.get("ok") and rec.get("guard"):
        return {
            "type": "shell",
            "command": cmd,
            "passed": False,
            "error": rec.get("error"),
            "guard": rec.get("guard"),
        }

    if rec.get("timed_out"):
        return {
            "type": "shell",
            "command": cmd,
            "passed": False,
            "timed_out": True,
            "error": rec.get("error") or "command timed out",
            "stdout": rec.get("stdout", "")[-2000:],
            "stderr": rec.get("stderr", "")[-2000:],
            "guard": rec.get("guard"),
        }

    exit_code = rec.get("exit_code", 1)
    ok = exit_code == expect
    return {
        "type": "shell",
        "command": cmd,
        "passed": ok,
        "exit_code": exit_code,
        "expected_exit": expect,
        "stdout": rec.get("stdout", "")[-2000:],
        "stderr": rec.get("stderr", "")[-2000:],
        "guard": rec.get("guard"),
        "error": None if ok else (rec.get("error") or f"exit {exit_code} != {expect}"),
    }


def _check_file_contains(workspace: str | None, spec: dict[str, Any]) -> dict[str, Any]:
    rel = spec.get("path", "")
    needle = spec.get("contains")
    if needle is None:
        needle = spec.get("substr")
    if needle is None:
        needle = ""
    needle = str(needle)
    base = _check_file_exists(workspace, spec)
    if not base.get("passed"):
        return {**base, "type": "file_contains", "contains": needle}
    if not needle:
        return {
            "type": "file_contains",
            "path": rel,
            "contains": needle,
            "passed": False,
            "error": "empty contains/substr",
        }
    root = Path(workspace).resolve()
    content = (root / rel).read_text(encoding="utf-8")
    ok = needle in content
    return {
        "type": "file_contains",
        "path": rel,
        "contains": needle,
        "passed": ok,
        "error": None if ok else "substring not found",
    }


_CHECKERS = {
    "file_exists": _check_file_exists,
    "shell": _check_shell,
    "file_contains": _check_file_contains,
}


def check_node_acceptance(
    node: dict[str, Any],
    workspace: str | None,
    llm_status: str,
) -> dict[str, Any]:
    """Run optional mechanical acceptance_checks on a node."""
    checks = node.get("acceptance_checks") or []
    if not checks:
        return {
            "passed": llm_status == "success",
            "skipped": True,
            "reason": "no acceptance_checks defined; using LLM status only",
            "results": [],
        }

    results: list[dict[str, Any]] = []
    for spec in checks:
        typ = spec.get("type", "")
        fn = _CHECKERS.get(typ)
        if not fn:
            results.append({"type": typ, "passed": False, "error": f"unknown check type: {typ}"})
            continue
        try:
            results.append(fn(workspace, spec))
        except Exception as exc:
            results.append({"type": typ, "passed": False, "error": str(exc)})

    passed = all(r.get("passed") for r in results)
    return {"passed": passed, "skipped": False, "results": results}
