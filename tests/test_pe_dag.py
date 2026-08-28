"""Tests for DAG execution planning."""

from planner_exec.pe_dag import next_executable_node, phase_execution_complete


def _dag(*nodes):
    return {"nodes": list(nodes)}


def _exec(node_id: str, status: str) -> dict:
    return {"node_id": node_id, "status": status}


def test_next_node_execute_when_deps_met():
    dag = _dag(
        {"id": "n1", "description": "a", "acceptance": "ok"},
        {"id": "n2", "description": "b", "acceptance": "ok", "reads_from": ["n1"]},
    )
    nxt = next_executable_node(dag, [])
    assert nxt is not None
    assert nxt["action"] == "execute"
    assert nxt["node"]["id"] == "n1"


def test_next_node_waits_for_upstream():
    dag = _dag(
        {"id": "n1", "description": "a", "acceptance": "ok"},
        {"id": "n2", "description": "b", "acceptance": "ok", "reads_from": ["n1"]},
    )
    nxt = next_executable_node(dag, [_exec("n1", "success")])
    assert nxt is not None
    assert nxt["node"]["id"] == "n2"


def test_max_retries_returns_blocked_not_none():
    dag = _dag({"id": "n1", "description": "a", "acceptance": "ok"})
    executions = [_exec("n1", "failed"), _exec("n1", "failed")]
    nxt = next_executable_node(dag, executions, max_retries=2)
    assert nxt is not None
    assert nxt["action"] == "blocked"
    assert nxt["node"]["id"] == "n1"


def test_all_done_returns_none():
    dag = _dag({"id": "n1", "description": "a", "acceptance": "ok"})
    assert next_executable_node(dag, [_exec("n1", "success")]) is None


def test_phase_complete_when_all_success():
    dag = _dag(
        {"id": "n1", "description": "a", "acceptance": "ok"},
        {"id": "n2", "description": "b", "acceptance": "ok", "reads_from": ["n1"]},
    )
    executions = [_exec("n1", "success"), _exec("n2", "success")]
    assert phase_execution_complete(dag, executions)


def test_phase_not_complete_when_blocked_node_failed():
    dag = _dag({"id": "n1", "description": "a", "acceptance": "ok"})
    assert not phase_execution_complete(dag, [_exec("n1", "failed")])
