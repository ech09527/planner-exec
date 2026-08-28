"""MCP/CLI JSON response size budgeting (Token Budget Layer C1)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

DEFAULT_MAX_RESPONSE_CHARS = int(os.environ.get("PE_MAX_RESPONSE_CHARS", "4000"))

PRESERVE_KEYS = frozenset({"task_id", "status", "blocked", "ok", "error"})
TRUNCATE_KEYS = ("phases", "entries", "steps", "detail", "meta", "progress", "nodes", "context")


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def response_chars(payload: dict[str, Any]) -> int:
    return len(_serialize(payload).encode("utf-8"))


def _summarize_field(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"_truncated": True, "summary": f"{name}: {len(value)} items"}
    if isinstance(value, dict):
        return {"_truncated": True, "summary": f"{name}: {len(value)} keys"}
    return {"_truncated": True, "summary": f"{name}: omitted"}


def fetch_hints(payload: dict[str, Any], truncated_fields: list[str]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    task_id = payload.get("task_id")
    if not task_id:
        return hints
    if "entries" in truncated_fields:
        hints.append(
            {
                "tool": "planner_query_logs",
                "params": {"task_id": task_id, "limit": 20, "failures_only": True},
            }
        )
    if "phases" in truncated_fields or "steps" in truncated_fields:
        hints.append(
            {
                "tool": "planner_query_logs",
                "params": {"task_id": task_id, "log_types": "execution,progress", "limit": 10},
            }
        )
    if "progress" in truncated_fields:
        hints.append({"tool": "planner_status", "params": {"task_id": task_id}})
    if payload.get("status") == "blocked":
        hints.append({"tool": "planner_replan_packet", "params": {"task_id": task_id}})
    return hints


def budget_json(payload: dict[str, Any], *, max_chars: int | None = None) -> dict[str, Any]:
    """Apply hard cap; attach unified _budget metadata."""
    cap = max_chars if max_chars is not None else DEFAULT_MAX_RESPONSE_CHARS
    original_chars = response_chars(payload)

    if original_chars <= cap:
        out = deepcopy(payload)
        out["_budget"] = {
            "truncated": False,
            "original_chars": original_chars,
            "returned_chars": original_chars,
            "truncated_fields": [],
            "fetch_hints": [],
        }
        return out

    work = deepcopy(payload)
    truncated_fields: list[str] = []

    for key in TRUNCATE_KEYS:
        if key not in work:
            continue
        work[key] = _summarize_field(key, work[key])
        truncated_fields.append(key)
        if response_chars(work) <= cap:
            break

    if response_chars(work) > cap:
        work = {k: v for k, v in work.items() if k in PRESERVE_KEYS or k in ("live", "task_id", "status", "blocked")}
        if "live" in work and isinstance(work["live"], dict):
            live = dict(work["live"])
            msg = live.get("latest_message")
            if msg is not None and len(str(msg)) > 120:
                live["latest_message"] = str(msg)[:120]
            work["live"] = live

    returned_chars = response_chars(work)
    work["_budget"] = {
        "truncated": True,
        "original_chars": original_chars,
        "returned_chars": returned_chars,
        "truncated_fields": truncated_fields,
        "fetch_hints": fetch_hints(payload, truncated_fields),
    }
    return work


def emit_json_response(
    payload: Any,
    *,
    budget: bool = False,
    max_chars: int | None = None,
    ledger: dict[str, str] | None = None,
) -> None:
    """Single choke point for JSON CLI/MCP stdout responses."""
    if budget and isinstance(payload, dict):
        payload = budget_json(payload, max_chars=max_chars)
    if ledger and isinstance(payload, dict) and ledger.get("task_id"):
        from .pe_token import record_mcp_response

        record_mcp_response(ledger["task_id"], ledger.get("tool", "unknown"), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def extract_blocked_from_phase_result(result: dict[str, Any]) -> dict[str, Any] | None:
    """Map phase run result to compact blocked summary."""
    if result.get("status") == "blocked":
        return {
            "phase": result.get("phase"),
            "node_id": result.get("node_id"),
            "reason": result.get("message", "blocked"),
        }
    steps = result.get("steps") or []
    for step in reversed(steps):
        if step.get("status") == "blocked" or step.get("escalate"):
            return {
                "phase": result.get("phase"),
                "node_id": step.get("node_id"),
                "reason": step.get("message") or result.get("message", "blocked"),
            }
    if result.get("status") == "failed" and steps:
        last = steps[-1]
        return {
            "phase": result.get("phase"),
            "node_id": last.get("node_id"),
            "reason": last.get("message") or result.get("message", "phase failed"),
        }
    if result.get("status") == "failed":
        return {
            "phase": result.get("phase"),
            "node_id": None,
            "reason": result.get("message", "phase failed"),
        }
    return None


def slim_phase_result(result: dict[str, Any], *, include_steps: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "task_id": result.get("task_id"),
        "phase": result.get("phase"),
        "status": result.get("status"),
        "stage": result.get("stage"),
        "message": result.get("message"),
        "execution_complete": result.get("execution_complete"),
    }
    blocked = extract_blocked_from_phase_result(result)
    if blocked:
        out["status"] = "blocked"
        out["blocked"] = blocked
    if include_steps:
        out["steps"] = result.get("steps")
    return {k: v for k, v in out.items() if v is not None}


def summarize_task_run(
    results: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    if not results:
        return "incomplete", None
    if all(r.get("status") == "completed" for r in results):
        return "completed", None
    for r in reversed(results):
        blocked = extract_blocked_from_phase_result(r)
        if blocked:
            return "blocked", blocked
    return "incomplete", None
