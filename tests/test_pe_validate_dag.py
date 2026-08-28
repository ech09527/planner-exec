"""Tests for v2 DAG validation."""

import pytest

from planner_exec.pe_validate import validate_dag_nodes


def _minimal_dag(*nodes):
    return {"nodes": list(nodes)}


def test_v2_dag_accepts_minimal_node():
    validate_dag_nodes(
        _minimal_dag(
            {
                "id": "n1",
                "description": "create hello.py",
                "acceptance": "hello.py exists",
            }
        )
    )


def test_v2_dag_accepts_reads_from_and_checks():
    validate_dag_nodes(
        _minimal_dag(
            {"id": "n1", "description": "step one", "acceptance": "done"},
            {
                "id": "n2",
                "description": "step two",
                "acceptance": "done",
                "reads_from": ["n1"],
                "acceptance_checks": [{"type": "file_exists", "path": "out.txt"}],
            },
        )
    )


def test_v2_dag_rejects_missing_acceptance():
    with pytest.raises(SystemExit, match="acceptance"):
        validate_dag_nodes(
            _minimal_dag({"id": "n1", "description": "do thing", "acceptance": ""})
        )


def test_v2_dag_rejects_unknown_reads_from():
    with pytest.raises(SystemExit, match="unknown node"):
        validate_dag_nodes(
            _minimal_dag(
                {
                    "id": "n1",
                    "description": "do thing",
                    "acceptance": "ok",
                    "reads_from": ["missing"],
                }
            )
        )


def test_v2_dag_rejects_bad_acceptance_check_type():
    with pytest.raises(SystemExit, match="unknown type"):
        validate_dag_nodes(
            _minimal_dag(
                {
                    "id": "n1",
                    "description": "do thing",
                    "acceptance": "ok",
                    "acceptance_checks": [{"type": "magic", "path": "x"}],
                }
            )
        )
