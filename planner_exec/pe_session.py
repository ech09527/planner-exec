"""Host session pointer storage (C3)."""

from __future__ import annotations

from typing import Any

from . import db
from .pe_orchestrate import compute_progress
from .pe_progress import build_live_status
from .pe_util import utc_now


def recommended_next(task_id: str, session: dict[str, Any] | None) -> str:
    meta = db.get_task_meta(task_id)
    status = (meta.get("status") or "").lower()
    poll_count = int((session or {}).get("poll_count") or 0)

    if db.get_latest_escalation(task_id) or any(
        marker in status for marker in ("blocked", "failed", "escalat", "eval_failed", "execute_failed")
    ):
        return "planner_replan_packet"

    if db.is_task_running(task_id):
        if poll_count < 60:
            return "planner_status"
        return "planner_query_logs"

    progress = compute_progress(task_id)
    if progress.get("next_action") == "all phases complete — finalize status":
        return "planner_token_report"

    return "planner_status"


def get_session_view(task_id: str) -> dict[str, Any]:
    session = db.get_task_session(task_id) or {}
    progress = compute_progress(task_id)
    live = build_live_status(task_id, progress)
    return {
        "task_id": task_id,
        "session": {
            "last_since": session.get("last_since"),
            "last_status_line": session.get("last_status_line") or live.get("status_line"),
            "poll_count": session.get("poll_count", 0),
            "updated_at": session.get("updated_at"),
        },
        "recommended_next": recommended_next(task_id, session),
        "live": live,
    }


def set_session_view(
    task_id: str,
    *,
    last_since: str | None = None,
    last_status_line: str | None = None,
    increment_poll: bool = False,
) -> dict[str, Any]:
    existing = db.get_task_session(task_id) or {}
    poll_count = int(existing.get("poll_count") or 0)
    if increment_poll:
        poll_count += 1
    db.upsert_task_session(
        task_id,
        last_since=last_since,
        last_status_line=last_status_line,
        poll_count=poll_count,
        updated_at=utc_now(),
    )
    return get_session_view(task_id)
