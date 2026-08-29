"""Tests for whole-DAG evaluation (mechanical + plan/run gates)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from planner_exec import db
from planner_exec.pe_dag_eval import (
    ensure_dag_eval_passed,
    evaluate_plan_dags,
    invalidate_dag_eval,
    mechanical_dag_eval,
)
from planner_exec.pe_orchestrate import internal_run_phase
from planner_exec.pe_plan import apply_plan


def _goal():
    return {
        "goal": "calc with tests",
        "success_criteria": ["unittest 通过"],
        "resources": [],
        "constraints": [],
        "assumptions": [],
        "open_questions": [],
    }


def _phases():
    return {
        "phases": [
            {
                "id": "p1",
                "title": "impl",
                "objective": "write",
                "inputs": [],
                "outputs": [],
                "done_definition": "done",
            }
        ]
    }


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(db, "ROOT", Path(tmp))
        db.init_db()
        yield


def test_mechanical_ok_when_later_node_has_shell():
    dags = [
        {
            "phase": 1,
            "nodes": [
                {
                    "id": "n5",
                    "description": "写 tests/test_calc.py，unittest 测试 add/mul",
                    "acceptance": "file exists",
                    "acceptance_checks": [{"type": "file_exists", "path": "tests/test_calc.py"}],
                },
                {
                    "id": "n6",
                    "description": "集成验收",
                    "acceptance": "unittest ok",
                    "acceptance_checks": [
                        {
                            "type": "shell",
                            "command": "python3 -m unittest discover -s tests -q",
                            "expect_exit": 0,
                        }
                    ],
                },
            ],
        }
    ]
    out = mechanical_dag_eval(goal=_goal(), phases=_phases()["phases"], dags=dags)
    assert out["passed"] is True


def test_mechanical_blocks_ungrounded_integration():
    dags = [
        {
            "phase": 1,
            "nodes": [
                {
                    "id": "n1",
                    "description": "写 tests 并跑 unittest",
                    "acceptance": "file exists",
                    "acceptance_checks": [{"type": "file_exists", "path": "tests/t.py"}],
                }
            ],
        }
    ]
    out = mechanical_dag_eval(goal=_goal(), phases=_phases()["phases"], dags=dags)
    assert out["passed"] is False
    assert any(i["type"] == "ungrounded_acceptance" for i in out["issues"])


def test_apply_plan_ready_with_skip_llm(temp_db, monkeypatch):
    monkeypatch.setenv("PE_SKIP_DAG_LLM", "1")
    plan = {
        "goal": "hello",
        "goal_confirmed": {
            "goal": "hello",
            "success_criteria": ["hello.py exists"],
            "resources": [],
            "constraints": [],
            "assumptions": [],
            "open_questions": [],
        },
        "phases": _phases(),
        "dags": [
            {
                "phase": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "description": "write hello.py",
                        "acceptance": "exists",
                        "acceptance_checks": [{"type": "file_exists", "path": "hello.py"}],
                    }
                ],
            }
        ],
    }
    result = apply_plan(plan, workspace="/tmp/ws", skip_dag_llm=True)
    assert result["ready_for_run"] is True
    assert result["dag_eval"]["passed"] is True
    assert db.get_artifact(result["task_id"], "dag-eval")["passed"] is True


def test_apply_plan_not_ready_on_mechanical_fail(temp_db):
    plan = {
        "goal": "bad",
        "goal_confirmed": _goal(),
        "phases": _phases(),
        "dags": [
            {
                "phase": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "description": "写并跑 unittest",
                        "acceptance": "exists",
                        "acceptance_checks": [{"type": "file_exists", "path": "t.py"}],
                    }
                ],
            }
        ],
    }
    result = apply_plan(plan, workspace="/tmp/ws", skip_dag_llm=True)
    assert result["ready_for_run"] is False
    assert result["status"] == "dag_eval_failed"
    assert result["dag_eval"]["passed"] is False


def test_run_blocked_when_dag_eval_stale(temp_db, monkeypatch):
    monkeypatch.setenv("PE_SKIP_DAG_LLM", "1")
    plan = {
        "goal": "hello",
        "goal_confirmed": {
            "goal": "hello",
            "success_criteria": ["ok"],
            "resources": [],
            "constraints": [],
            "assumptions": [],
            "open_questions": [],
        },
        "phases": _phases(),
        "dags": [
            {
                "phase": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "description": "write hello.py",
                        "acceptance": "exists",
                        "acceptance_checks": [{"type": "file_exists", "path": "hello.py"}],
                    }
                ],
            }
        ],
    }
    result = apply_plan(plan, workspace="/tmp/ws", skip_dag_llm=True)
    tid = result["task_id"]
    invalidate_dag_eval(tid)

    # ensure re-eval can pass again with skip
    monkeypatch.setattr(
        "planner_exec.pe_dag_eval.evaluate_dag_with_llm",
        lambda _ctx: {"passed": True, "skipped": True, "issues": [], "suggestions": []},
    )
    # After invalidate, ensure_dag_eval re-runs; with skip_llm env it should pass
    gate = ensure_dag_eval_passed(tid)
    assert gate["passed"] is True

    # Force fail gate path in run
    invalidate_dag_eval(tid)
    monkeypatch.setattr(
        "planner_exec.pe_orchestrate.ensure_dag_eval_passed",
        lambda _tid: {
            "passed": False,
            "cached": False,
            "blocker_count": 1,
            "issues": [{"severity": "blocker", "type": "x", "message": "bad"}],
            "suggestions": [],
        },
    )
    out = internal_run_phase(tid, 1)
    assert out["status"] == "blocked"
    assert out["stage"] == "dag_eval"


def test_evaluate_plan_dags_llm_fail_blocks():
    dags = [
        {
            "phase": 1,
            "nodes": [
                {
                    "id": "n1",
                    "description": "write a.py",
                    "acceptance": "exists",
                    "acceptance_checks": [{"type": "file_exists", "path": "a.py"}],
                }
            ],
        }
    ]

    def fake_llm(_ctx):
        return {
            "passed": False,
            "skipped": False,
            "issues": [{"severity": "blocker", "type": "dag_quality", "message": "bad cut"}],
            "suggestions": ["merge nodes"],
        }

    import planner_exec.pe_dag_eval as m

    old = m.evaluate_dag_with_llm
    m.evaluate_dag_with_llm = fake_llm
    try:
        out = evaluate_plan_dags(
            goal={"goal": "x", "success_criteria": []},
            phases_doc=_phases(),
            dags=dags,
            skip_llm=False,
        )
        assert out["passed"] is False
        assert out["blocker_count"] >= 1
    finally:
        m.evaluate_dag_with_llm = old
