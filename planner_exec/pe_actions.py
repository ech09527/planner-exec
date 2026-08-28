"""Apply worker LLM actions inside workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pe_shell import run_guarded_shell


def apply_actions(
    actions: list[dict[str, Any]],
    workspace: str | None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not actions:
        return results

    root = Path(workspace).resolve() if workspace else None
    if not root or not root.exists():
        return [{"ok": False, "error": f"workspace not found: {workspace}"}]

    for action in actions:
        typ = action.get("type")
        if typ == "write_file":
            rel = action.get("path", "")
            target = (root / rel).resolve()
            if not str(target).startswith(str(root)):
                results.append({"ok": False, "type": typ, "error": f"path escapes workspace: {rel}"})
                continue
            if dry_run:
                results.append({"ok": True, "type": typ, "path": str(target), "dry_run": True})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.get("content", ""), encoding="utf-8")
            results.append({"ok": True, "type": typ, "path": str(target)})

        elif typ == "run_shell":
            cmd = action.get("command", "")
            if not cmd:
                results.append({"ok": False, "type": typ, "error": "empty command"})
                continue
            results.append(
                run_guarded_shell(
                    str(root),
                    cmd,
                    timeout=int(action.get("timeout", 120)),
                    dry_run=dry_run,
                )
            )
        else:
            results.append({"ok": False, "type": typ, "error": f"unsupported action: {typ}"})

    return results
