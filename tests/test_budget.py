"""Tests for Token Budget Layer (C1)."""

import io
import json
from contextlib import redirect_stdout

from planner_exec.pe_budget import (
    budget_json,
    emit_json_response,
    extract_blocked_from_phase_result,
    response_chars,
    slim_phase_result,
    summarize_task_run,
)


def test_budget_passthrough_small_payload():
    payload = {"task_id": "t1", "status": "completed", "live": {"latest_message": "ok"}}
    out = budget_json(payload, max_chars=4000)
    assert out["_budget"]["truncated"] is False
    assert out["_budget"]["fetch_hints"] == []
    assert out["task_id"] == "t1"


def test_budget_truncates_large_phases():
    phases = [
        {
            "phase": i,
            "status": "completed",
            "steps": [{"node_id": f"n{j}", "message": "x" * 300} for j in range(15)],
        }
        for i in range(1, 6)
    ]
    payload = {
        "task_id": "t1",
        "status": "incomplete",
        "live": {"latest_message": "running"},
        "phases": phases,
    }
    out = budget_json(payload, max_chars=800)
    assert out["_budget"]["truncated"] is True
    assert "phases" in out["_budget"]["truncated_fields"]
    assert out["phases"]["_truncated"] is True
    assert out["_budget"]["fetch_hints"]
    assert out["_budget"]["returned_chars"] <= 800


def test_budget_truncates_query_logs_entries():
    entries = [
        {"summary": f"execution:n{i}", "detail": {"blob": "y" * 500}}
        for i in range(30)
    ]
    payload = {"task_id": "t1", "returned": 30, "entries": entries}
    out = budget_json(payload, max_chars=600)
    assert out["_budget"]["truncated"] is True
    assert "entries" in out["_budget"]["truncated_fields"]
    assert any(h["tool"] == "planner_query_logs" for h in out["_budget"]["fetch_hints"])


def test_emit_json_response_adds_budget():
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_json_response({"task_id": "t1", "status": "ok"}, budget=True, max_chars=4000)
    data = json.loads(buf.getvalue())
    assert "_budget" in data
    assert data["_budget"]["truncated"] is False


def test_slim_phase_result_omits_steps():
    result = {
        "task_id": "t1",
        "phase": 1,
        "status": "failed",
        "stage": "execute",
        "message": "phase failed",
        "steps": [{"node_id": "n2", "status": "failed", "message": "shell exit 1"}],
    }
    slim = slim_phase_result(result)
    assert "steps" not in slim
    assert slim["status"] == "blocked"
    assert slim["blocked"]["node_id"] == "n2"


def test_slim_phase_result_include_steps():
    result = {"task_id": "t1", "phase": 1, "status": "completed", "steps": [{"node_id": "n1"}]}
    slim = slim_phase_result(result, include_steps=True)
    assert slim["steps"] == result["steps"]


def test_summarize_task_run_blocked():
    results = [
        {"phase": 1, "status": "completed", "steps": []},
        {
            "phase": 2,
            "status": "failed",
            "steps": [{"node_id": "n3", "status": "failed", "message": "timeout"}],
        },
    ]
    status, blocked = summarize_task_run(results)
    assert status == "blocked"
    assert blocked["node_id"] == "n3"


def test_extract_blocked_escalate_step():
    result = {
        "phase": 1,
        "status": "incomplete",
        "steps": [{"node_id": "n1", "status": "ok"}, {"node_id": "n2", "escalate": True, "message": "stuck"}],
    }
    blocked = extract_blocked_from_phase_result(result)
    assert blocked is not None
    assert blocked["node_id"] == "n2"


def test_response_chars_uses_utf8_byte_length():
    payload = {"task_id": "测试", "status": "ok"}
    assert response_chars(payload) == len(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    )
