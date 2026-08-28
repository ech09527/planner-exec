#!/usr/bin/env python3
"""MCP server for planner-exec.

Premium agent: plan via MCP tools (init, save phases/dag).
Cheap LLM tier: eval-phase + run-phase inside this server.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Any

from mcp.server.mcpserver import MCPServer

from planner_exec import pe
from planner_exec.pe_mcp import debug_tools_enabled, observe_tools_enabled


def _run_pe(func, args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(args)
    return buf.getvalue().strip()


SERVER_INSTRUCTIONS = """
Planner-Exec MCP — 主 Agent 规划，MCP 内 pydantic-ai agent 验证与执行

推荐流程：
1. planner_init → task_id
2. planner_save(goal-confirmed / phases / dag) — 自然语言节点 + 可选 acceptance_checks
3. planner_run_task 或 planner_run_phase — 自动 eval + 执行（默认瘦返回 + _budget 元数据）
4. 失败时 planner_replan_packet → planner_patch_node → 重跑
5. planner_status / planner_query_logs — 查进度；planner_token_report — 用量

节点 schema v2：id + description + acceptance + reads_from/depends_on
可选 acceptance_checks: [{type: file_exists|shell|file_contains, ...}]

默认 core 工具：init, save, run_task, run_phase, status, query_logs, replan_packet, patch_node, token_report, session_get/set
调试工具需 PE_MCP_DEBUG_TOOLS=1；list/migrate/show/progress 需 PE_MCP_OBSERVE_TOOLS=1

不要逐节点 planner_execute_node；不要替 MCP 做节点验证。
响应超 PE_MAX_RESPONSE_CHARS 时自动截断，见 _budget.fetch_hints。
""".strip()

server = MCPServer(
    name="planner-exec",
    title="Planner Exec",
    description="Plan goals with premium agent; execute phase DAGs via cheap LLM worker.",
    instructions=SERVER_INSTRUCTIONS,
    version="0.3.0",
)


@server.tool(
    name="planner_init",
    description="Create a new task. Returns task_id (required for all later calls).",
)
def planner_init(
    goal: str,
    context: dict[str, Any] | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    workspace: str | None = None,
    max_node_eval_iterations: int = 3,
    max_node_execute_retries: int = 2,
) -> str:
    args = argparse.Namespace(
        goal=goal,
        context=json.dumps(context or {}, ensure_ascii=False),
        context_file=None,
        task_id=task_id,
        source="mcp",
        agent_id=agent_id,
        workspace=workspace,
        force=False,
        max_node_eval_iterations=max_node_eval_iterations,
        max_node_execute_retries=max_node_execute_retries,
    )
    return _run_pe(pe.cmd_init, args)


@server.tool(
    name="planner_save",
    description="Save task artifact: goal-confirmed | phases | dag | execution | status.",
)
def planner_save(
    task_id: str,
    artifact_type: str,
    data: dict[str, Any],
    phase: int | None = None,
) -> str:
    args = argparse.Namespace(
        task_id=task_id,
        type=artifact_type,
        data=json.dumps(data, ensure_ascii=False),
        data_file=None,
        phase=phase,
    )
    return _run_pe(pe.cmd_save, args)


@server.tool(
    name="planner_run_phase",
    description=(
        "Run full phase: eval all nodes then execute DAG via cheap LLM. "
        "Default slim response (no steps[]). Use include_steps=true for legacy payload."
    ),
)
def planner_run_phase(
    task_id: str,
    phase: int,
    skip_eval: bool = False,
    mechanical_only: bool = False,
    include_steps: bool = False,
) -> str:
    return _run_pe(
        pe.cmd_run_phase,
        argparse.Namespace(
            task_id=task_id,
            phase=phase,
            skip_eval=skip_eval,
            mechanical_only=mechanical_only,
            include_steps=include_steps,
        ),
    )


@server.tool(name="planner_status", description="Compact task status with phase progress and latest event.")
def planner_status(task_id: str) -> str:
    return _run_pe(pe.cmd_status, argparse.Namespace(task_id=task_id))


@server.tool(
    name="planner_run_task",
    description=(
        "Run all phases: eval + execute each phase until done or blocked. "
        "Default slim response. Use include_phases=true for legacy phases[] payload."
    ),
)
def planner_run_task(
    task_id: str,
    from_phase: int = 1,
    skip_eval: bool = False,
    mechanical_only: bool = False,
    include_phases: bool = False,
) -> str:
    return _run_pe(
        pe.cmd_run_task,
        argparse.Namespace(
            task_id=task_id,
            from_phase=from_phase,
            skip_eval=skip_eval,
            mechanical_only=mechanical_only,
            include_phases=include_phases,
        ),
    )


@server.tool(
    name="planner_query_logs",
    description=(
        "Query unified task execution logs (eval, execution, escalation, progress, agent traces). "
        "Use since= for polling during run_task; failures_only=true for replanning. "
        "detail=true for full payloads."
    ),
)
def planner_query_logs(
    task_id: str,
    phase: int | None = None,
    node: str | None = None,
    log_types: str | None = None,
    limit: int = 20,
    detail: bool = False,
    since: str | None = None,
    offset: int = 0,
    failures_only: bool = False,
) -> str:
    return _run_pe(
        pe.cmd_query_logs,
        argparse.Namespace(
            task_id=task_id,
            phase=phase,
            node=node,
            log_types=log_types,
            limit=limit,
            detail=detail,
            since=since,
            offset=offset,
            failures_only=failures_only,
        ),
    )


@server.tool(
    name="planner_replan_packet",
    description="Minimal replan package after escalation or blocked run_task. Call before revising DAG.",
)
def planner_replan_packet(task_id: str) -> str:
    return _run_pe(pe.cmd_replan_packet, argparse.Namespace(task_id=task_id))


@server.tool(
    name="planner_patch_node",
    description="Incrementally patch DAG nodes: replace, insert_after, delete.",
)
def planner_patch_node(
    task_id: str,
    phase: int,
    patches: list[dict[str, Any]],
) -> str:
    return _run_pe(
        pe.cmd_patch_node,
        argparse.Namespace(
            task_id=task_id,
            phase=phase,
            data=json.dumps({"patches": patches}, ensure_ascii=False),
            data_file=None,
        ),
    )


@server.tool(
    name="planner_token_report",
    description="Token/char usage report. internal_llm is not billed to main agent.",
)
def planner_token_report(
    task_id: str,
    host_input_tokens: int | None = None,
    host_output_tokens: int | None = None,
) -> str:
    return _run_pe(
        pe.cmd_token_report,
        argparse.Namespace(
            task_id=task_id,
            host_input_tokens=host_input_tokens,
            host_output_tokens=host_output_tokens,
        ),
    )


@server.tool(
    name="planner_session_get",
    description="Read host session pointers (last_since, status_line) and recommended_next tool.",
)
def planner_session_get(task_id: str) -> str:
    return _run_pe(pe.cmd_session_get, argparse.Namespace(task_id=task_id))


@server.tool(
    name="planner_session_set",
    description="Update host session pointers after polling planner_status or query_logs.",
)
def planner_session_set(
    task_id: str,
    last_since: str | None = None,
    last_status_line: str | None = None,
    increment_poll: bool = False,
) -> str:
    return _run_pe(
        pe.cmd_session_set,
        argparse.Namespace(
            task_id=task_id,
            last_since=last_since,
            last_status_line=last_status_line,
            increment_poll=increment_poll,
        ),
    )


def _register_observe_tools() -> None:
    @server.tool(name="planner_list", description="List tasks with optional filters.")
    def planner_list(
        query: str | None = None,
        status: str | None = None,
        workspace: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        return _run_pe(
            pe.cmd_list,
            argparse.Namespace(query=query, status=status, workspace=workspace, agent_id=agent_id),
        )

    @server.tool(name="planner_show", description="Task metadata, progress, and LLM availability.")
    def planner_show(task_id: str) -> str:
        return _run_pe(pe.cmd_show, argparse.Namespace(task_id=task_id))

    @server.tool(name="planner_progress", description="Compact progress summary with next_action.")
    def planner_progress(task_id: str) -> str:
        return _run_pe(pe.cmd_progress, argparse.Namespace(task_id=task_id))

    @server.tool(
        name="planner_migrate",
        description="Import legacy JSON task directories from ~/.planer-exec/tasks/ into SQLite.",
    )
    def planner_migrate(task_id: str | None = None, import_all: bool = False) -> str:
        return _run_pe(pe.cmd_migrate, argparse.Namespace(task_id=task_id, all=import_all))


def _register_debug_tools() -> None:
    @server.tool(
        name="planner_eval_node",
        description="Evaluate a single DAG node (mechanical + cheap LLM). Debug only.",
    )
    def planner_eval_node(
        task_id: str,
        phase: int,
        node: str,
        mechanical_only: bool = False,
        force: bool = False,
    ) -> str:
        return _run_pe(
            pe.cmd_eval_node,
            argparse.Namespace(
                task_id=task_id,
                phase=phase,
                node=node,
                iteration=None,
                mechanical_only=mechanical_only,
                force=force,
            ),
        )

    @server.tool(
        name="planner_eval_phase",
        description="Evaluate all nodes in a phase via cheap LLM. Debug only.",
    )
    def planner_eval_phase(
        task_id: str,
        phase: int,
        node: str | None = None,
        mechanical_only: bool = False,
        force: bool = False,
    ) -> str:
        return _run_pe(
            pe.cmd_eval_phase,
            argparse.Namespace(
                task_id=task_id,
                phase=phase,
                node=node,
                mechanical_only=mechanical_only,
                force=force,
            ),
        )

    @server.tool(name="planner_eval_status", description="Summarize node eval status for a phase. Debug only.")
    def planner_eval_status(task_id: str, phase: int) -> str:
        return _run_pe(pe.cmd_eval_status, argparse.Namespace(task_id=task_id, phase=phase))

    @server.tool(
        name="planner_execute_node",
        description="Execute next or specified DAG node via cheap LLM worker. Debug only.",
    )
    def planner_execute_node(
        task_id: str,
        phase: int,
        node: str | None = None,
        dry_run: bool = False,
    ) -> str:
        return _run_pe(
            pe.cmd_execute_node,
            argparse.Namespace(task_id=task_id, phase=phase, node=node, dry_run=dry_run),
        )

    @server.tool(name="planner_next_node", description="Show next executable node without running. Debug only.")
    def planner_next_node(task_id: str, phase: int) -> str:
        return _run_pe(pe.cmd_next_node, argparse.Namespace(task_id=task_id, phase=phase))


if observe_tools_enabled():
    _register_observe_tools()

if debug_tools_enabled():
    _register_debug_tools()


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
