"""Tests for progress events and query_logs observability."""

import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_progress import build_live_status, emit_progress


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", Path(tmp))
        db.init_db()
        now = "2026-08-28T00:00:00Z"
        db.create_task(
            {
                "task_id": "t1",
                "workspace": "/tmp/ws",
                "agent_id": None,
                "status": "initialized",
                "max_node_eval_iterations": 3,
                "max_node_execute_retries": 2,
                "created_at": now,
                "updated_at": now,
            },
            {"goal": "test goal", "context": {}},
        )
        yield "t1"


def test_emit_progress_and_latest(temp_db):
    task_id = temp_db
    ts = emit_progress(task_id, "node_start", phase=1, node_id="n1", status="running")
    latest = db.get_latest_progress_event(task_id)
    assert latest is not None
    assert latest["event"] == "node_start"
    assert latest["node_id"] == "n1"
    assert latest["saved_at"] == ts


def test_query_logs_since_filter(temp_db):
    task_id = temp_db
    db.append_status_snapshot(
        task_id,
        {"kind": "progress", "event": "phase_start", "phase": 1, "status": "running"},
        "2026-08-28T00:00:01Z",
    )
    db.append_status_snapshot(
        task_id,
        {"kind": "progress", "event": "node_done", "phase": 1, "node_id": "n1", "status": "success"},
        "2026-08-28T00:00:02Z",
    )

    result = db.query_task_logs(task_id, log_types=["progress"], since="2026-08-28T00:00:01Z")
    assert result["returned"] == 1
    assert result["entries"][0]["summary"].startswith("progress:node_done")

    result_all = db.query_task_logs(task_id, log_types=["progress"], limit=10)
    assert result_all["returned"] == 2


def test_query_logs_failures_only(temp_db):
    task_id = temp_db
    now = "2026-08-28T01:00:00Z"
    db.append_execution(
        task_id,
        1,
        {
            "node_id": "n1",
            "status": "failed",
            "error": "boom",
            "saved_at": now,
            "phase": 1,
        },
    )
    emit_progress(task_id, "node_done", phase=1, node_id="n1", status="success")

    result = db.query_task_logs(task_id, failures_only=True, log_types=["execution", "progress"])
    kinds = {e["type"] for e in result["entries"]}
    assert "execution" in kinds
    assert "progress" not in kinds or all(e.get("status") == "failed" for e in result["entries"] if e["type"] == "progress")


def test_replan_hints_execution_failure(temp_db):
    task_id = temp_db
    now = "2026-08-28T02:00:00Z"
    db.append_execution(
        task_id,
        1,
        {
            "node_id": "n1",
            "status": "failed",
            "error": "test failure",
            "acceptance_mechanical": {
                "passed": False,
                "results": [{"type": "file_exists", "passed": False, "error": "file not found"}],
            },
            "saved_at": now,
            "phase": 1,
        },
    )
    result = db.query_task_logs(task_id, log_types=["execution"])
    kinds = {h["kind"] for h in result["replan_hints"]}
    assert "execution_failure" in kinds
    assert "acceptance_failure" in kinds


def test_build_live_status(temp_db):
    task_id = temp_db
    emit_progress(task_id, "phase_start", phase=1, status="running")
    progress = {
        "next_action": "run phase 1",
        "phases": [
            {
                "phase_index": 1,
                "execution_complete": False,
                "nodes": [
                    {"node_id": "n1", "execution_status": "success"},
                    {"node_id": "n2", "execution_status": None},
                ],
            }
        ],
    }
    live = build_live_status(task_id, progress)
    assert live["current_phase"] == 1
    assert live["nodes_done"] == 1
    assert live["nodes_total"] == 2
    assert "status_line" in live
