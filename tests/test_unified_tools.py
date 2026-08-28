"""Tests for unified Core tools (scheme A)."""

import json
import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_cli import cmd_replan, cmd_run, cmd_status


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", Path(tmp))
        db.init_db()
        now = "2026-08-28T00:00:00Z"
        task_id = "t-unified"
        db.create_task(
            {
                "task_id": task_id,
                "workspace": "/tmp/ws",
                "agent_id": None,
                "status": "phase_01_blocked",
                "max_node_eval_iterations": 3,
                "max_node_execute_retries": 2,
                "created_at": now,
                "updated_at": now,
            },
            {"goal": "test", "context": {}},
        )
        dag = {
            "nodes": [
                {
                    "id": "n1",
                    "description": "step",
                    "acceptance": "done",
                }
            ]
        }
        from planner_exec.pe_dag import dag_revision

        rev = dag_revision(dag)
        db.save_phase_dag(task_id, 1, rev, {**dag, "dag_revision": rev, "phase": 1}, now)
        yield task_id


def test_status_includes_recommended_next(temp_db, capsys):
    import argparse

    cmd_status(argparse.Namespace(task_id=temp_db, last_since=None, last_status_line=None, increment_poll=False))
    out = json.loads(capsys.readouterr().out)
    assert "recommended_next" in out
    assert "live" in out
    assert "session" in out


def test_replan_packet_without_patches(temp_db, capsys):
    import argparse

    cmd_replan(argparse.Namespace(task_id=temp_db, phase=None, data=None, data_file=None, patches=None))
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == temp_db
    assert "blocked" in out


def test_replan_apply_patches(temp_db, capsys):
    import argparse

    cmd_replan(
        argparse.Namespace(
            task_id=temp_db,
            phase=1,
            data=None,
            data_file=None,
            patches=[{"op": "replace", "node_id": "n1", "node": {"description": "updated"}}],
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["changed_nodes"] == ["n1"]


def test_run_task_without_phases_exits(temp_db):
    import argparse

    with pytest.raises(SystemExit):
        cmd_run(
            argparse.Namespace(
                task_id=temp_db,
                phase=None,
                from_phase=1,
                skip_eval=True,
                mechanical_only=True,
                include_steps=False,
                include_phases=False,
            )
        )
