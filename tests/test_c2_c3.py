"""Tests for C2 replan packet, patch_node, token ledger, and C3 session."""

import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_budget import response_chars
from planner_exec.pe_patch import PatchError, apply_node_patches, task_allows_patch
from planner_exec.pe_progress import emit_progress
from planner_exec.pe_replan import build_replan_packet
from planner_exec.pe_session import get_session_view, set_session_view
from planner_exec.pe_token import get_token_report, record_mcp_response


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", Path(tmp))
        db.init_db()
        now = "2026-08-28T00:00:00Z"
        task_id = "t-c2"
        db.create_task(
            {
                "task_id": task_id,
                "workspace": "/tmp/ws",
                "agent_id": None,
                "status": "phase_01_n1_execute_failed",
                "max_node_eval_iterations": 3,
                "max_node_execute_retries": 2,
                "created_at": now,
                "updated_at": now,
            },
            {"goal": "build hello world", "context": {}},
        )
        db.save_artifact(
            task_id,
            "goal-confirmed",
            {
                "goal": "build hello world",
                "success_criteria": ["hello.py runs"],
                "resources": [],
                "constraints": [],
                "assumptions": [],
                "open_questions": [],
            },
            now,
        )
        db.save_artifact(
            task_id,
            "phases",
            {
                "phases": [
                    {
                        "id": "p1",
                        "title": "Phase 1",
                        "objective": "create file",
                        "inputs": [],
                        "outputs": [],
                        "done_definition": "done",
                    }
                ]
            },
            now,
        )
        dag = {
            "nodes": [
                {
                    "id": "n1",
                    "description": "create hello.py",
                    "acceptance": "file exists",
                    "acceptance_checks": [{"type": "file_exists", "path": "hello.py"}],
                }
            ]
        }
        from planner_exec.pe_dag import dag_revision

        rev = dag_revision(dag)
        db.save_phase_dag(task_id, 1, rev, {**dag, "dag_revision": rev, "phase": 1}, now)
        yield task_id


@pytest.fixture()
def temp_db_multi(monkeypatch, temp_db):
    task_id = temp_db
    now = "2026-08-28T00:00:00Z"
    dag = {
        "nodes": [
            {
                "id": "n1",
                "description": "setup",
                "acceptance": "setup done",
            },
            {
                "id": "n2",
                "description": "run hello",
                "acceptance": "hello runs",
                "reads_from": ["n1"],
            },
        ]
    }
    from planner_exec.pe_dag import dag_revision

    rev = dag_revision(dag)
    db.save_phase_dag(task_id, 1, rev, {**dag, "dag_revision": rev, "phase": 1}, now)
    return task_id


def test_build_replan_packet_after_escalation(temp_db):
    task_id = temp_db
    db.write_escalation(
        task_id,
        1,
        {"node_id": "n1", "reason": "max_retries_exceeded", "hint": "replan"},
        "2026-08-28T01:00:00Z",
    )
    db.append_execution(
        task_id,
        1,
        {
            "node_id": "n1",
            "status": "failed",
            "error": "shell exit 1",
            "saved_at": "2026-08-28T01:00:01Z",
            "acceptance_mechanical": {
                "passed": False,
                "results": [{"type": "shell", "command": "python hello.py", "passed": False, "error": "exit 1"}],
            },
        },
    )

    packet = build_replan_packet(task_id)
    assert packet["task_id"] == task_id
    assert packet["blocked"]["node_id"] == "n1"
    assert packet["failure"]["kind"] in ("escalation", "acceptance_failure", "execution_failure")
    assert packet["_estimated_tokens"] > 0
    assert response_chars({k: v for k, v in packet.items() if k != "_budget"}) <= 2500


def test_patch_node_replace_bumps_revision(temp_db):
    task_id = temp_db
    assert task_allows_patch(task_id)[0] is True

    old_dag = db.get_phase_dag(task_id, 1)
    old_rev = old_dag["dag_revision"]

    result = apply_node_patches(
        task_id,
        1,
        [{"op": "replace", "node_id": "n1", "node": {"description": "create hello.py with print"}}],
    )
    assert result["ok"] is True
    assert result["changed_nodes"] == ["n1"]
    assert result["dag_revision"] != old_rev

    new_dag = db.get_phase_dag(task_id, 1)
    assert new_dag["nodes"][0]["description"] == "create hello.py with print"


def test_patch_node_rejects_while_running(temp_db):
    task_id = temp_db
    emit_progress(task_id, "phase_start", phase=1, status="running")
    with pytest.raises(PatchError) as exc:
        apply_node_patches(task_id, 1, [{"op": "replace", "node_id": "n1", "node": {"description": "x"}}])
    assert exc.value.status == 409


def test_patch_node_rejects_unsupported_op(temp_db):
    task_id = temp_db
    with pytest.raises(PatchError) as exc:
        apply_node_patches(task_id, 1, [{"op": "merge", "node_id": "n1"}])
    assert "unsupported" in str(exc.value).lower()


def test_patch_node_insert_after(temp_db_multi):
    task_id = temp_db_multi
    result = apply_node_patches(
        task_id,
        1,
        [
            {
                "op": "insert_after",
                "after": "n1",
                "node": {
                    "id": "n1b",
                    "description": "intermediate check",
                    "acceptance": "check passes",
                },
            }
        ],
    )
    assert result["changed_nodes"] == ["n1b"]
    dag = db.get_phase_dag(task_id, 1)
    ids = [n["id"] for n in dag["nodes"]]
    assert ids == ["n1", "n1b", "n2"]


def test_patch_node_delete_leaf(temp_db_multi):
    task_id = temp_db_multi
    result = apply_node_patches(task_id, 1, [{"op": "delete", "node_id": "n2"}])
    assert result["changed_nodes"] == ["n2"]
    dag = db.get_phase_dag(task_id, 1)
    assert [n["id"] for n in dag["nodes"]] == ["n1"]


def test_patch_node_delete_rejects_referenced_node(temp_db_multi):
    task_id = temp_db_multi
    with pytest.raises(PatchError) as exc:
        apply_node_patches(task_id, 1, [{"op": "delete", "node_id": "n1"}])
    assert "referenced" in str(exc.value).lower()


def test_patch_node_delete_last_node_rejected(temp_db):
    task_id = temp_db
    with pytest.raises(PatchError) as exc:
        apply_node_patches(task_id, 1, [{"op": "delete", "node_id": "n1"}])
    assert "last node" in str(exc.value).lower()


def test_budget_blocked_hint():
    from planner_exec.pe_budget import fetch_hints

    hints = fetch_hints({"task_id": "t1", "status": "blocked"}, [])
    assert any(h["tool"] == "planner_replan_packet" for h in hints)


def test_session_poll_simulation(temp_db):
    task_id = temp_db
    for _ in range(5):
        set_session_view(task_id, increment_poll=True)
    view = get_session_view(task_id)
    assert view["session"]["poll_count"] == 5


def test_token_report_mcp_and_internal(temp_db):
    task_id = temp_db
    record_mcp_response(task_id, "planner_status", {"task_id": task_id, "status": "ok"})
    db.record_token_ledger(
        task_id,
        source="internal_llm",
        tool_or_role="execute",
        input_tokens=100,
        output_tokens=50,
        response_chars=0,
        meta={},
        created_at="2026-08-28T02:00:00Z",
    )

    report = get_token_report(task_id)
    assert report["totals"]["mcp_responses_est"] > 0
    assert report["totals"]["internal_llm"] == 150
    assert "planner_status" in report["totals"]["by_tool"]


def test_session_get_set_and_recommended_next(temp_db):
    task_id = temp_db
    view = set_session_view(
        task_id,
        last_since="2026-08-28T00:00:01Z",
        last_status_line="phase=1",
        increment_poll=True,
    )
    assert view["session"]["poll_count"] == 1
    assert view["recommended_next"] == "planner_replan_packet"

    again = get_session_view(task_id)
    assert again["session"]["last_since"] == "2026-08-28T00:00:01Z"
