"""Unit tests for mechanical acceptance_checks."""

from pathlib import Path

from planner_exec.pe_acceptance import check_node_acceptance, _check_file_contains


def test_file_contains_accepts_contains(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello world", encoding="utf-8")
    out = _check_file_contains(str(tmp_path), {"path": "a.txt", "contains": "hello"})
    assert out["passed"] is True
    assert out["contains"] == "hello"


def test_file_contains_accepts_substr_alias(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello world", encoding="utf-8")
    out = _check_file_contains(str(tmp_path), {"path": "a.txt", "substr": "world"})
    assert out["passed"] is True
    assert out["contains"] == "world"


def test_file_contains_empty_needle_fails(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    out = _check_file_contains(str(tmp_path), {"path": "a.txt", "contains": ""})
    assert out["passed"] is False
    assert "empty" in (out.get("error") or "")


def test_file_contains_missing_needle_fails(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    out = _check_file_contains(str(tmp_path), {"path": "a.txt"})
    assert out["passed"] is False


def test_check_node_acceptance_authoritative(tmp_path: Path):
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    node = {
        "id": "n1",
        "acceptance_checks": [{"type": "file_exists", "path": "ok.txt"}],
    }
    out = check_node_acceptance(node, str(tmp_path), "failed")
    assert out["skipped"] is False
    assert out["passed"] is True
