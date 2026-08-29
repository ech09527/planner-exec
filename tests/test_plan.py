"""Tests for planner_plan (atomic init + save)."""

import json
import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_cli import cmd_plan
from planner_exec.pe_plan import PlanError, apply_plan


def _sample_plan() -> dict:
    return {
        "goal": "build hello world",
        "goal_confirmed": {
            "goal": "build hello world",
            "success_criteria": ["hello.py runs"],
            "resources": [],
            "constraints": [],
            "assumptions": [],
            "open_questions": [],
        },
        "phases": {
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
        "dags": [
            {
                "phase": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "description": "create hello.py",
                        "acceptance": "file exists",
                        "acceptance_checks": [{"type": "file_exists", "path": "hello.py"}],
                    }
                ],
            }
        ],
    }


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", Path(tmp))
        monkeypatch.setenv("PE_SKIP_DAG_LLM", "1")
        db.init_db()
        yield


def test_apply_plan_writes_artifacts(temp_db):
    plan = _sample_plan()
    result = apply_plan(plan, workspace="/tmp/ws")
    task_id = result["task_id"]
    assert result["ready_for_run"] is True
    assert result["summary"]["node_count"] == 1
    assert "description" not in json.dumps(result)
    assert db.get_artifact(task_id, "goal-confirmed") is not None
    assert db.get_phase_dag(task_id, 1) is not None


def test_apply_plan_validate_only_no_write(temp_db):
    plan = _sample_plan()
    result = apply_plan(plan, validate_only=True)
    assert result["validate_only"] is True
    assert not db.task_exists(result["task_id"])


def test_apply_plan_rejects_mismatched_dag_count(temp_db):
    plan = _sample_plan()
    plan["dags"] = []
    with pytest.raises(PlanError):
        apply_plan(plan)


def test_cmd_plan_cli(temp_db, capsys):
    import argparse

    cmd_plan(
        argparse.Namespace(
            plan=json.dumps(_sample_plan()),
            plan_file=None,
            task_id="my-task-1",
            workspace="/tmp/ws",
            agent_id=None,
            force=False,
            validate_only=False,
            source="test",
            max_node_eval_iterations=3,
            max_node_execute_retries=2,
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "my-task-1"
    assert out["_budget"]["truncated"] is False
    assert db.task_exists("my-task-1")
