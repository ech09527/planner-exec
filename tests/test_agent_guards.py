"""Unit tests for agent failure classification and validate issue filtering."""

from planner_exec.pe_agent import (
    EvalIssue,
    _check_tool_repeat,
    _filter_validate_issues,
    classify_agent_failure,
    AgentDeps,
)
from planner_exec.pe_llm import _needs_openai_response_compat


def test_classify_step_limit():
    assert classify_agent_failure("request_limit of 30") == "agent_step_limit"
    assert classify_agent_failure("exceed the request_limit") == "agent_step_limit"


def test_classify_rate_limit():
    assert classify_agent_failure("status_code: 429 rate limit") == "llm_rate_limited"


def test_classify_unavailable():
    assert classify_agent_failure("no LLM API key") == "llm_unavailable"


def test_filter_missing_workspace_blockers():
    issues = [
        EvalIssue(severity="blocker", type="ambiguity", message="workspace 为空，calc/add.py 不存在"),
        EvalIssue(severity="blocker", type="ambiguity", message="description 含糊不清"),
    ]
    filtered, no_blockers = _filter_validate_issues(issues)
    assert not no_blockers
    assert filtered[0].severity == "warning"
    assert filtered[1].severity == "blocker"


def test_filter_only_missing_passes():
    issues = [EvalIssue(severity="blocker", type="x", message="file not found: calc/add.py")]
    filtered, no_blockers = _filter_validate_issues(issues)
    assert no_blockers
    assert filtered[0].severity == "warning"


def test_tool_repeat_stops():
    deps = AgentDeps(task_id=None, phase=1, node_id="n1", agent_role="execute")
    assert _check_tool_repeat(deps, "read_file", path="a.py") is None
    assert _check_tool_repeat(deps, "read_file", path="a.py") is None
    stop = _check_tool_repeat(deps, "read_file", path="a.py")
    assert stop is not None
    assert "STOP" in stop
    assert deps.stop_requested


def test_openai_compat_default_on_for_proxy():
    assert _needs_openai_response_compat("http://10.9.159.83:8787/v1") is True
    assert _needs_openai_response_compat("https://api.openai.com/v1") is False
