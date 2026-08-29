"""Internal worker agents powered by pydantic-ai.

Validate agent: read-only tools, structured ValidateResult.
Execute agent: read/write tools, structured ExecuteResult.
Each tool call and run step is logged to agent_traces for planner_query_logs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from . import db
from .pe_actions import apply_actions
from .pe_llm import load_llm_config, build_pe_model

DEFAULT_MAX_STEPS = int(os.environ.get("PE_AGENT_MAX_STEPS", "15"))
TRACE_MESSAGES = os.environ.get("PE_AGENT_TRACE_MESSAGES", "").lower() in ("1", "true", "yes")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvalIssue(BaseModel):
    severity: Literal["blocker", "warning"] = "blocker"
    type: str = "ambiguity"
    message: str


class ValidateResult(BaseModel):
    passed: bool
    issues: list[EvalIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class ExecuteResult(BaseModel):
    status: Literal["success", "failed"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    acceptance_check: list[str] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None


@dataclass
class AgentDeps:
    task_id: str | None
    phase: int | None
    node_id: str | None
    agent_role: str
    workspace: str | None = None
    upstream_nodes: dict[str, Any] = field(default_factory=dict)
    action_log: list[dict[str, Any]] = field(default_factory=list)
    recent_tool_keys: list[str] = field(default_factory=list)
    stop_requested: bool = False
    _step: int = 0

    def trace(self, event_type: str, data: dict[str, Any]) -> None:
        if not self.task_id:
            return
        self._step += 1
        db.append_agent_trace(
            task_id=self.task_id,
            agent_role=self.agent_role,
            event_type=event_type,
            data=data,
            created_at=_utc_now(),
            phase=self.phase,
            node_id=self.node_id,
            step=self._step,
        )


TOOL_REPEAT_LIMIT = int(os.environ.get("PE_AGENT_TOOL_REPEAT_LIMIT", "3"))

_MISSING_WORKSPACE_MARKERS = (
    "file not found",
    "not a directory",
    "no such file",
    "不存在",
    "尚未",
    "workspace 为空",
    "workspace is empty",
    "目录不存在",
    "无 calc",
    "empty workspace",
)


def classify_agent_failure(exc: BaseException | str) -> str:
    text = str(exc).lower()
    if any(x in text for x in ("request_limit", "usage limit", "tool_calls_limit", "exceed the request")):
        return "agent_step_limit"
    if any(x in text for x in ("429", "rate limit", "too many requests")):
        return "llm_rate_limited"
    if any(x in text for x in ("api key", "authentication", "401", "unauthorized", "no llm")):
        return "llm_unavailable"
    return "agent_error"


def _is_missing_workspace_issue(message: str) -> bool:
    m = message.lower()
    return any(marker in m for marker in _MISSING_WORKSPACE_MARKERS)


def _filter_validate_issues(issues: list[EvalIssue]) -> tuple[list[EvalIssue], bool]:
    """Downgrade pre-execute workspace-missing blockers to warnings."""
    filtered: list[EvalIssue] = []
    for issue in issues:
        if issue.severity == "blocker" and _is_missing_workspace_issue(issue.message):
            filtered.append(
                EvalIssue(
                    severity="warning",
                    type="workspace_pre_execute",
                    message=f"{issue.message} （忽略：eval 在 execute 前运行，产物尚未创建是正常的）",
                )
            )
        else:
            filtered.append(issue)
    passed = not any(i.severity == "blocker" for i in filtered)
    return filtered, passed


def _check_tool_repeat(deps: AgentDeps, tool: str, **parts: Any) -> str | None:
    key = tool + "|" + "|".join(f"{k}={parts[k]}" for k in sorted(parts))
    deps.recent_tool_keys.append(key)
    n = 0
    for prev in reversed(deps.recent_tool_keys):
        if prev == key:
            n += 1
        else:
            break
    if n >= TOOL_REPEAT_LIMIT:
        deps.stop_requested = True
        return (
            f"STOP: repeated {tool}({parts}) {n} times. "
            "Do NOT call this tool again. Return your final structured result NOW."
        )
    return None


def _workspace_root(ctx: RunContext[AgentDeps]) -> Path:
    if not ctx.deps.workspace:
        raise ValueError("workspace not configured")
    root = Path(ctx.deps.workspace).resolve()
    if not root.exists():
        raise ValueError(f"workspace not found: {ctx.deps.workspace}")
    return root


def _safe_path(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"path escapes workspace: {rel}")
    return target


def _register_read_tools(agent: Agent[AgentDeps, Any]) -> None:
    @agent.tool
    def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
        """Read a UTF-8 text file relative to workspace."""
        stop = _check_tool_repeat(ctx.deps, "read_file", path=path)
        if stop:
            ctx.deps.trace("tool", {"tool": "read_file", "path": path, "ok": False, "error": stop, "repeat_stop": True})
            return stop
        try:
            target = _safe_path(_workspace_root(ctx), path)
            if not target.is_file():
                msg = f"file not found: {path}"
                ctx.deps.trace("tool", {"tool": "read_file", "path": path, "ok": False, "error": msg})
                return msg
            content = target.read_text(encoding="utf-8")
            ctx.deps.trace(
                "tool",
                {"tool": "read_file", "path": path, "ok": True, "bytes": len(content.encode("utf-8"))},
            )
            return content[:12000]
        except Exception as exc:
            ctx.deps.trace("tool", {"tool": "read_file", "path": path, "ok": False, "error": str(exc)})
            return f"ERROR: {exc}"

    @agent.tool
    def list_dir(ctx: RunContext[AgentDeps], path: str = ".") -> str:
        """List files and directories under a workspace-relative path."""
        stop = _check_tool_repeat(ctx.deps, "list_dir", path=path)
        if stop:
            ctx.deps.trace("tool", {"tool": "list_dir", "path": path, "ok": False, "error": stop, "repeat_stop": True})
            return stop
        try:
            target = _safe_path(_workspace_root(ctx), path)
            if not target.is_dir():
                msg = f"not a directory: {path}"
                ctx.deps.trace("tool", {"tool": "list_dir", "path": path, "ok": False, "error": msg})
                return msg
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines = [f"{'[dir]' if e.is_dir() else '[file]'} {e.name}" for e in entries[:200]]
            ctx.deps.trace("tool", {"tool": "list_dir", "path": path, "ok": True, "count": len(lines)})
            return "\n".join(lines) or "(empty)"
        except Exception as exc:
            ctx.deps.trace("tool", {"tool": "list_dir", "path": path, "ok": False, "error": str(exc)})
            return f"ERROR: {exc}"

    @agent.tool
    def get_upstream_summary(ctx: RunContext[AgentDeps]) -> str:
        """Summarize upstream DAG nodes (reads_from / depends_on context)."""
        stop = _check_tool_repeat(ctx.deps, "get_upstream_summary")
        if stop:
            ctx.deps.trace("tool", {"tool": "get_upstream_summary", "ok": False, "error": stop, "repeat_stop": True})
            return stop
        if not ctx.deps.upstream_nodes:
            ctx.deps.trace("tool", {"tool": "get_upstream_summary", "ok": True, "nodes": 0})
            return "No upstream nodes."
        parts: list[str] = []
        for nid, node in ctx.deps.upstream_nodes.items():
            parts.append(
                f"## {nid}: {node.get('title', '')}\n"
                f"description: {node.get('description', '')}\n"
                f"acceptance: {node.get('acceptance', '')}"
            )
        summary = "\n\n".join(parts)
        ctx.deps.trace(
            "tool",
            {"tool": "get_upstream_summary", "ok": True, "nodes": list(ctx.deps.upstream_nodes)},
        )
        return summary


def _register_write_tools(agent: Agent[AgentDeps, Any]) -> None:
    @agent.tool
    def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
        """Write UTF-8 text to a workspace-relative path."""
        stop = _check_tool_repeat(ctx.deps, "write_file", path=path)
        if stop:
            ctx.deps.trace("tool", {"tool": "write_file", "path": path, "ok": False, "error": stop, "repeat_stop": True})
            return stop
        results = apply_actions(
            [{"type": "write_file", "path": path, "content": content}],
            ctx.deps.workspace,
        )
        ctx.deps.action_log.extend(results)
        ok = bool(results and results[0].get("ok"))
        ctx.deps.trace("tool", {"tool": "write_file", "path": path, "ok": ok, "result": results[0] if results else {}})
        if not ok:
            return f"ERROR: {(results[0] if results else {}).get('error', 'write failed')}"
        return f"Wrote {path}. If acceptance is satisfied, return final structured result NOW (do not re-read the same file)."

    @agent.tool
    def run_shell(ctx: RunContext[AgentDeps], command: str, timeout: int = 120) -> str:
        """Run a shell command inside workspace (use for tests/builds)."""
        stop = _check_tool_repeat(ctx.deps, "run_shell", command=command)
        if stop:
            ctx.deps.trace("tool", {"tool": "run_shell", "command": command, "ok": False, "error": stop, "repeat_stop": True})
            return stop
        results = apply_actions(
            [{"type": "run_shell", "command": command, "timeout": timeout}],
            ctx.deps.workspace,
        )
        ctx.deps.action_log.extend(results)
        rec = results[0] if results else {"ok": False, "error": "no result"}
        guard = rec.get("guard") or {}
        ctx.deps.trace(
            "tool",
            {
                "tool": "run_shell",
                "command": command,
                "ok": rec.get("ok"),
                "guard_level": guard.get("level"),
                "guard_source": guard.get("source"),
                "result": rec,
            },
        )
        if not rec.get("ok"):
            err = rec.get("error") or "command failed"
            return (
                f"ERROR: {err}\n"
                f"EXIT {rec.get('exit_code')}\n"
                f"stderr: {rec.get('stderr', '')}\n"
                f"stdout: {rec.get('stdout', '')}"
            )
        return f"EXIT 0\nstdout: {rec.get('stdout', '')}"


VALIDATE_SYSTEM = """你是 DAG 单节点验证 agent（只读）。在 execute 之前运行。

职责（仅检查计划质量，不是检查 workspace 是否已有产物）：
1. description 是否清晰、可执行（写「将要做什么」，而非「文件已存在」）
2. 与上游 reads_from/depends_on 衔接是否合理
3. acceptance / acceptance_checks 是否可机械验证

硬规则：
- workspace 里还没有本节点产物是**正常**的，绝不能因此给出 blocker / passed=false
- 不要反复 list_dir / read_file；最多各用 1 次，优先 get_upstream_summary
- 工具返回 STOP 后立即输出结构化结论，禁止再调工具
- 默认倾向 passed=true；仅当描述含糊、缺验收、依赖矛盾时才 blocker

输出：passed + issues + suggestions。"""

EXECUTE_SYSTEM = """你是 DAG 单节点执行 agent。

职责：按 description 在 workspace 完成工作，满足 acceptance。
工具：read_file / list_dir / get_upstream_summary / write_file / run_shell。

硬规则：
1. 写完目标文件并确认 acceptance 后，**立即**返回 status=success，禁止继续工具循环
2. 同一路径不要重复 read_file / write_file；工具返回 STOP 后必须立刻输出最终结果
3. 不要超出本节点边界；中间节点优先写文件，复杂 shell/unittest 留给验收节点
4. 不要假设未提供的输入

输出：status、outputs、acceptance_check、summary。"""


def _build_validate_agent() -> Agent[AgentDeps, ValidateResult]:
    agent: Agent[AgentDeps, ValidateResult] = Agent(
        OpenAIChatModel("gpt-4o-mini", provider=OpenAIProvider(api_key="placeholder")),
        deps_type=AgentDeps,
        output_type=ValidateResult,
        system_prompt=VALIDATE_SYSTEM,
    )
    _register_read_tools(agent)
    return agent


def _build_execute_agent() -> Agent[AgentDeps, ExecuteResult]:
    agent: Agent[AgentDeps, ExecuteResult] = Agent(
        OpenAIChatModel("gpt-4o-mini", provider=OpenAIProvider(api_key="placeholder")),
        deps_type=AgentDeps,
        output_type=ExecuteResult,
        system_prompt=EXECUTE_SYSTEM,
    )
    _register_read_tools(agent)
    _register_write_tools(agent)
    return agent


_validate_agent = _build_validate_agent()
_execute_agent = _build_execute_agent()


def _usage_limits() -> UsageLimits:
    return UsageLimits(request_limit=DEFAULT_MAX_STEPS, tool_calls_limit=DEFAULT_MAX_STEPS * 3)


def _run_usage_dump(result: Any) -> dict[str, Any]:
    usage = result.usage() if callable(getattr(result, "usage", None)) else result.usage
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return {}


def _trace_messages(deps: AgentDeps, messages: list[Any], role: str) -> None:
    if not TRACE_MESSAGES:
        return
    for msg in messages:
        data = msg.model_dump() if hasattr(msg, "model_dump") else {"raw": str(msg)}
        deps.trace("message", {"role": role, "message": data})


def _validate_prompt(context: dict[str, Any]) -> str:
    payload = {
        "node": context.get("node"),
        "upstream_nodes": context.get("upstream_nodes"),
        "goal_success_criteria": context.get("goal_success_criteria"),
        "phase": context.get("phase"),
        "workspace": context.get("workspace"),
    }
    return "请验证以下 DAG 节点（只读工具可用）：\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _execute_prompt(context: dict[str, Any]) -> str:
    payload = {
        "node": context.get("node"),
        "inputs": context.get("inputs"),
        "workspace": context.get("workspace"),
        "upstream_nodes": context.get("upstream_nodes"),
        "phase": context.get("phase"),
    }
    return "请执行以下 DAG 节点（工具可在 workspace 内读写/跑命令）：\n\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )


def _meta_from_context(context: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    task_id = context.get("task_id")
    phase = context.get("phase_number")
    if phase is None and isinstance(context.get("phase"), dict):
        phase = context["phase"].get("id")
    node = context.get("node") or {}
    node_id = node.get("id")
    return task_id, phase, node_id


def evaluate_node_with_agent(context: dict[str, Any]) -> dict[str, Any]:
    cfg = load_llm_config("eval")
    if not cfg["api_key"]:
        return {
            "passed": None,
            "skipped": True,
            "reason": "no LLM API key (set PE_LLM_API_KEY)",
            "issues": [],
            "suggestions": [],
            "model": None,
            "executor": "pydantic-ai",
        }

    task_id, phase, node_id = _meta_from_context(context)
    deps = AgentDeps(
        task_id=task_id,
        phase=phase,
        node_id=node_id,
        agent_role="validate",
        workspace=context.get("workspace"),
        upstream_nodes=context.get("upstream_nodes") or {},
    )
    deps.trace("run_start", {"context_keys": list(context.keys())})

    user_prompt = _validate_prompt(context)

    try:
        result = _validate_agent.run_sync(
            user_prompt,
            deps=deps,
            model=build_pe_model(cfg),
            usage_limits=_usage_limits(),
        )
        out = result.output
        _trace_messages(deps, result.new_messages(), "validate")
        filtered_issues, no_blockers = _filter_validate_issues(list(out.issues))
        # Empty issues → trust model; otherwise remaining blockers decide.
        passed = bool(out.passed) if not out.issues else no_blockers
        deps.trace(
            "run_end",
            {
                "passed": passed,
                "issue_count": len(filtered_issues),
                "usage": _run_usage_dump(result),
            },
        )
        if task_id:
            from .pe_token import record_internal_llm_from_usage

            usage = _run_usage_dump(result)
            record_internal_llm_from_usage(
                task_id,
                "validate",
                usage,
                phase=phase,
                node_id=node_id,
                model=cfg["model"],
            )
        return {
            "passed": passed,
            "skipped": False,
            "issues": [i.model_dump() for i in filtered_issues],
            "suggestions": out.suggestions,
            "model": cfg["model"],
            "executor": "pydantic-ai",
        }
    except Exception as exc:
        deps.trace("run_error", {"error": str(exc)})
        fail_reason = classify_agent_failure(exc)
        if fail_reason == "agent_step_limit":
            # Step budget exhausted while probing workspace — do not block the plan.
            return {
                "passed": True,
                "skipped": False,
                "fail_reason": fail_reason,
                "reason": f"validate agent hit step limit: {exc}",
                "issues": [
                    {
                        "severity": "warning",
                        "type": "agent_step_limit",
                        "message": str(exc),
                    }
                ],
                "suggestions": ["Validate agent hit step limit; mechanical plan checks still apply."],
                "model": cfg["model"],
                "executor": "pydantic-ai",
            }
        return {
            "passed": None,
            "skipped": True,
            "fail_reason": fail_reason,
            "reason": f"validate agent failed: {exc}",
            "issues": [],
            "suggestions": [],
            "model": cfg["model"],
            "executor": "pydantic-ai",
        }


def execute_node_with_agent(context: dict[str, Any]) -> dict[str, Any]:
    cfg = load_llm_config("execute")
    if not cfg["api_key"]:
        return {
            "status": "failed",
            "skipped": True,
            "reason": "no LLM API key (set PE_LLM_API_KEY)",
            "outputs": {},
            "acceptance_check": [],
            "actions": [],
            "model": None,
            "executor": "pydantic-ai",
        }

    task_id, phase, node_id = _meta_from_context(context)
    node = context.get("node") or {}
    upstream: dict[str, Any] = {}
    for dep_id in node.get("reads_from") or node.get("depends_on") or []:
        if dep_id in (context.get("upstream_nodes") or {}):
            upstream[dep_id] = context["upstream_nodes"][dep_id]

    deps = AgentDeps(
        task_id=task_id,
        phase=phase,
        node_id=node_id,
        agent_role="execute",
        workspace=context.get("workspace"),
        upstream_nodes=upstream,
    )
    deps.trace("run_start", {"node_id": node_id, "workspace": context.get("workspace")})

    user_prompt = _execute_prompt(context)

    try:
        result = _execute_agent.run_sync(
            user_prompt,
            deps=deps,
            model=build_pe_model(cfg),
            usage_limits=_usage_limits(),
        )
        out = result.output
        _trace_messages(deps, result.new_messages(), "execute")
        deps.trace(
            "run_end",
            {
                "status": out.status,
                "summary": out.summary,
                "action_count": len(deps.action_log),
                "usage": _run_usage_dump(result),
            },
        )
        if task_id:
            from .pe_token import record_internal_llm_from_usage

            usage = _run_usage_dump(result)
            record_internal_llm_from_usage(
                task_id,
                "execute",
                usage,
                phase=phase,
                node_id=node_id,
                model=cfg["model"],
            )
        return {
            "status": out.status,
            "skipped": False,
            "outputs": out.outputs,
            "acceptance_check": out.acceptance_check,
            "actions": deps.action_log,
            "error": out.error,
            "summary": out.summary,
            "model": cfg["model"],
            "executor": "pydantic-ai",
        }
    except Exception as exc:
        deps.trace("run_error", {"error": str(exc)})
        fail_reason = classify_agent_failure(exc)
        # Keep actions so orchestrator can still pass mechanical acceptance.
        return {
            "status": "failed",
            "skipped": False,
            "fail_reason": fail_reason,
            "reason": f"execute agent failed: {exc}",
            "outputs": {},
            "acceptance_check": [],
            "actions": deps.action_log,
            "error": str(exc),
            "model": cfg["model"],
            "executor": "pydantic-ai",
        }
