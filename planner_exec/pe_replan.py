"""Replan packet builder for escalation recovery (C2)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import db
from .pe_budget import budget_json, response_chars
from .pe_node import slim_node_for_prompt
from .pe_token import chars_to_tokens

REPLAN_TARGET_CHARS = 2000  # ~500 tokens


def _pick_primary_hint(hints: list[dict[str, Any]]) -> dict[str, Any] | None:
    priority = ("escalation", "acceptance_failure", "execution_failure", "tool_failure")
    for kind in priority:
        for hint in hints:
            if hint.get("kind") == kind:
                return hint
    return hints[-1] if hints else None


def _find_node(dag: dict[str, Any] | None, node_id: str | None) -> dict[str, Any] | None:
    if not dag or not node_id:
        return None
    for node in dag.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def _latest_failed_execution(task_id: str, phase: int, node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for record in reversed(db.load_executions(task_id, phase)):
        if record.get("node_id") == node_id and record.get("status") == "failed":
            return record
    return None


def _build_failure_section(
    hint: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    escalation: dict[str, Any] | None,
) -> dict[str, Any]:
    kind = (hint or {}).get("kind") or "unknown"
    errors: list[str] = []
    last_tool_error: str | None = None

    if hint:
        if hint.get("error"):
            last_tool_error = f"{hint.get('tool')}: {hint['error']}"
        if hint.get("summary"):
            errors.append(str(hint["summary"]))

    if execution:
        mech = execution.get("acceptance_mechanical") or {}
        if mech and not mech.get("passed"):
            kind = "acceptance_failure"
            for result in mech.get("results") or []:
                if not result.get("passed"):
                    errors.append(str(result.get("error") or result.get("type")))
        elif execution.get("error"):
            errors.append(str(execution["error"]))

    if escalation:
        kind = "escalation"
        if escalation.get("reason"):
            errors.insert(0, str(escalation["reason"]))

    errors = [e for e in errors if e]
    return {
        "kind": kind,
        "errors": errors[:5],
        "last_tool_error": last_tool_error,
    }


def _suggested_patches(
    kind: str,
    node: dict[str, Any] | None,
    execution: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not node:
        return []
    node_id = node["id"]
    if kind == "acceptance_failure":
        checks: list[dict[str, Any]] = list(node.get("acceptance_checks") or [])
        if execution:
            mech = execution.get("acceptance_mechanical") or {}
            for result in mech.get("results") or []:
                if result.get("type") == "shell" and result.get("command"):
                    checks.append(
                        {
                            "type": "shell",
                            "command": result["command"],
                            "expect_exit": result.get("expect_exit", 0),
                        }
                    )
        if not checks and node.get("acceptance"):
            checks.append({"type": "file_exists", "path": "."})
        return [{"op": "replace", "node_id": node_id, "node": {"acceptance_checks": checks}}]
    if kind == "escalation":
        desc = (node.get("description") or "")[:200]
        return [
            {
                "op": "replace",
                "node_id": node_id,
                "node": {
                    "description": desc or "Simplify this step and retry with a smaller scope.",
                },
            },
            {
                "op": "insert_after",
                "after": node_id,
                "node": {
                    "id": f"{node_id}-retry",
                    "description": "Retry with a smaller scope after simplifying prerequisites.",
                    "acceptance": node.get("acceptance") or "step completes without error",
                },
            },
        ]
    return []


def _context_digest(task_id: str, phase: int, node_id: str | None) -> dict[str, Any]:
    goal = db.get_artifact(task_id, "goal-confirmed") or db.get_artifact(task_id, "goal-raw") or {}
    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phase_def = phases_doc["phases"][phase - 1] if 0 < phase <= len(phases_doc.get("phases", [])) else {}
    goal_line = str(goal.get("goal") or "")[:120]
    upstream_done: list[str] = []
    if node_id:
        executions = db.load_executions(task_id, phase)
        for record in executions:
            if record.get("status") in ("success", "skipped") and record.get("node_id"):
                if record["node_id"] != node_id:
                    upstream_done.append(record["node_id"])
    return {
        "goal_one_liner": goal_line,
        "phase_title": phase_def.get("title"),
        "upstream_done": upstream_done[-10:],
    }


def _degrade_packet(packet: dict[str, Any], max_chars: int) -> dict[str, Any]:
    work = deepcopy(packet)
    if response_chars(work) <= max_chars:
        return work
    work.pop("suggested_patches", None)
    if response_chars(work) <= max_chars:
        return work
    work.pop("context_digest", None)
    if response_chars(work) <= max_chars:
        return work
    failure = work.get("failure") or {}
    errors = failure.get("errors") or []
    if len(errors) > 1:
        failure["errors"] = errors[:1]
        work["failure"] = failure
    if response_chars(work) <= max_chars:
        return work
    return budget_json(work, max_chars=max_chars)


def build_replan_packet(task_id: str) -> dict[str, Any]:
    logs = db.query_task_logs(task_id, failures_only=True, limit=30)
    hints = logs.get("replan_hints") or []
    hint = _pick_primary_hint(hints)

    escalation = db.get_latest_escalation(task_id)
    phase = (hint or {}).get("phase") or (escalation or {}).get("phase") or 1
    node_id = (hint or {}).get("node_id") or (escalation or {}).get("node_id")

    dag = db.get_phase_dag(task_id, phase)
    node = _find_node(dag, node_id)
    execution = _latest_failed_execution(task_id, phase, node_id)

    reason = (escalation or {}).get("reason") or (hint or {}).get("summary") or "blocked"
    if escalation and escalation.get("reason"):
        reason = str(escalation["reason"])

    node_summary = None
    if node:
        slim = slim_node_for_prompt(node)
        node_summary = {
            "description": slim.get("description"),
            "acceptance": slim.get("acceptance"),
        }

    failure = _build_failure_section(hint, execution, escalation)
    kind = failure.get("kind") or "unknown"

    packet: dict[str, Any] = {
        "task_id": task_id,
        "blocked": {
            "phase": phase,
            "node_id": node_id,
            "reason": reason,
            "node_summary": node_summary,
        },
        "failure": failure,
        "suggested_patches": _suggested_patches(kind, node, execution),
        "context_digest": _context_digest(task_id, phase, node_id),
    }

    packet = _degrade_packet(packet, REPLAN_TARGET_CHARS)
    est_chars = response_chars({k: v for k, v in packet.items() if k != "_budget"})
    packet["_estimated_tokens"] = chars_to_tokens(est_chars)
    return packet
