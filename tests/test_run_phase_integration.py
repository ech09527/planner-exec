"""Golden-path integration tests for eval → execute with injectable LLM seams."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_orchestrate import (
    internal_eval_node,
    internal_execute_node,
    internal_run_phase,
)
from planner_exec.pe_plan import apply_plan


def _sample_plan(ws: str) -> dict:
    return {
        "goal": "write hello",
        "goal_confirmed": {
            "goal": "write hello",
            "success_criteria": ["hello.txt exists"],
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
                    "outputs": ["hello.txt"],
                    "done_definition": "file exists",
                }
            ]
        },
        "dags": [
            {
                "phase": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "description": "Write hello.txt with greeting text.",
                        "acceptance": "hello.txt exists and contains hello",
                        "acceptance_checks": [
                            {"type": "file_exists", "path": "hello.txt"},
                            {"type": "file_contains", "path": "hello.txt", "contains": "hello"},
                        ],
                    }
                ],
            }
        ],
        "workspace": ws,
    }


@pytest.fixture()
def task_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "test.db"
        ws = root / "ws"
        ws.mkdir()
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", root)
        monkeypatch.setenv("PE_SKIP_DAG_LLM", "1")
        db.init_db()
        result = apply_plan(_sample_plan(str(ws)), workspace=str(ws), skip_dag_llm=True)
        yield {"task_id": result["task_id"], "workspace": ws, "root": root}


def test_eval_fail_blocks_run_phase(task_env, monkeypatch):
    def fake_eval(_ctx):
        return {
            "passed": False,
            "skipped": False,
            "issues": [{"severity": "blocker", "type": "ambiguity", "message": "vague"}],
            "suggestions": [],
            "model": "mock",
            "executor": "mock",
        }

    monkeypatch.setattr(
        "planner_exec.pe_orchestrate.evaluate_node_with_llm",
        fake_eval,
    )
    out = internal_run_phase(task_env["task_id"], 1)
    assert out["status"] == "blocked"
    assert out["stage"] == "eval"
    assert out["all_passed"] is False


def test_happy_path_complete(task_env, monkeypatch):
    ws: Path = task_env["workspace"]

    def fake_eval(_ctx):
        return {
            "passed": True,
            "skipped": False,
            "issues": [],
            "suggestions": [],
            "model": "mock",
            "executor": "mock",
        }

    def fake_exec(_ctx):
        (ws / "hello.txt").write_text("hello\n", encoding="utf-8")
        return {
            "status": "success",
            "skipped": False,
            "outputs": {"hello.txt": "written"},
            "acceptance_check": ["ok"],
            "actions": [],
            "model": "mock",
            "executor": "mock",
        }

    monkeypatch.setattr("planner_exec.pe_orchestrate.evaluate_node_with_llm", fake_eval)
    monkeypatch.setattr("planner_exec.pe_orchestrate.execute_node_with_llm", fake_exec)

    out = internal_run_phase(task_env["task_id"], 1)
    assert out["status"] == "completed"
    assert out["execution_complete"] is True
    assert (ws / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_acceptance_overrides_llm_failed(task_env, monkeypatch):
    ws: Path = task_env["workspace"]
    (ws / "hello.txt").write_text("hello world", encoding="utf-8")

    def fake_eval(_ctx):
        return {
            "passed": True,
            "skipped": False,
            "issues": [],
            "suggestions": [],
            "model": "mock",
            "executor": "mock",
        }

    def fake_exec(_ctx):
        return {
            "status": "failed",
            "skipped": False,
            "outputs": {},
            "acceptance_check": [],
            "actions": [],
            "error": "model said fail",
            "model": "mock",
            "executor": "mock",
        }

    monkeypatch.setattr("planner_exec.pe_orchestrate.evaluate_node_with_llm", fake_eval)
    monkeypatch.setattr("planner_exec.pe_orchestrate.execute_node_with_llm", fake_exec)

    eval_out = internal_eval_node(task_env["task_id"], 1, "n1")
    assert eval_out["passed"] is True
    exec_out = internal_execute_node(task_env["task_id"], 1)
    assert exec_out["status"] == "success"
    assert exec_out["record"]["acceptance_mechanical"]["passed"] is True


def test_execute_blocked_without_eval_pass(task_env, monkeypatch):
    def fake_eval(_ctx):
        return {
            "passed": False,
            "skipped": False,
            "issues": [{"severity": "blocker", "type": "x", "message": "no"}],
            "suggestions": [],
            "model": "mock",
            "executor": "mock",
        }

    monkeypatch.setattr("planner_exec.pe_orchestrate.evaluate_node_with_llm", fake_eval)
    internal_eval_node(task_env["task_id"], 1, "n1")
    out = internal_execute_node(task_env["task_id"], 1)
    assert out["status"] == "blocked"
    assert "not eval-passed" in (out.get("message") or "")
