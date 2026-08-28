"""Run progress events for observability during long execute loops."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import db


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_progress(
    task_id: str,
    event: str,
    *,
    phase: int | None = None,
    node_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Persist a progress event and update task meta. Returns timestamp."""
    now = _utc_now()
    payload: dict[str, Any] = {
        "kind": "progress",
        "event": event,
        "phase": phase,
        "node_id": node_id,
        "status": status,
        "message": message,
    }
    if extra:
        payload.update(extra)

    db.append_status_snapshot(task_id, payload, now)

    meta_status = status or event
    if phase is not None and node_id:
        meta_status = f"phase_{phase:02d}_{node_id}_{meta_status}"
    elif phase is not None:
        meta_status = f"phase_{phase:02d}_{meta_status}"

    db.update_task_meta(task_id, updated_at=now, status=meta_status)
    return now


def build_live_status(task_id: str, progress: dict[str, Any]) -> dict[str, Any]:
    """Compact status for planner_status / polling."""
    latest = db.get_latest_progress_event(task_id) or {}
    meta = db.get_task_meta(task_id)

    current_phase = None
    nodes_done = 0
    nodes_total = 0
    for phase_info in progress.get("phases", []):
        if not phase_info.get("execution_complete"):
            current_phase = phase_info.get("phase_index")
            nodes_total = len(phase_info.get("nodes") or [])
            nodes_done = sum(
                1
                for n in phase_info.get("nodes") or []
                if n.get("execution_status") in ("success", "skipped")
            )
            break
        nodes_done = len(phase_info.get("nodes") or [])
        nodes_total = nodes_done

    status_line_parts = [task_id, f"status={meta.get('status')}"]
    if current_phase is not None:
        status_line_parts.append(f"phase={current_phase} ({nodes_done}/{nodes_total} nodes)")
    if latest.get("event"):
        status_line_parts.append(f"last={latest.get('event')}")
    status_line = " | ".join(status_line_parts)

    return {
        "task_id": task_id,
        "status_line": status_line,
        "status": meta.get("status"),
        "next_action": progress.get("next_action"),
        "current_phase": current_phase,
        "nodes_done": nodes_done,
        "nodes_total": nodes_total,
        "latest_event": latest.get("event"),
        "latest_node_id": latest.get("node_id"),
        "latest_message": latest.get("message"),
        "latest_at": latest.get("saved_at"),
    }
