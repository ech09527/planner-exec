#!/usr/bin/env python3
"""MCP server for planner-exec.

Core tools (5): plan, run, status, replan, query_logs
Observe: init, save, list, show, progress, migrate, token_report, legacy aliases
Debug: eval_*, execute_node, next_node
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
Planner-Exec MCP — 主 Agent 规划，MCP 内 pydantic-ai 验证与执行

Core 工具（5 个）：
1. planner_plan — 一次提交 goal + phases + dags，返回 task_id + summary（勿复述 nodes）
2. planner_run — 跑任务；phase=N 只跑单 phase
3. planner_status — 进度 + session + recommended_next
4. planner_replan — 无 patches 返回重规划包；有 patches 则 apply
5. planner_query_logs — 日志（limit=20，勿 detail=true）

推荐流程：
plan → run → [blocked] replan → replan(patches=...) → run

plan 包需含：goal, goal_confirmed, phases, dags[{phase, nodes[]}]
init/save 在 PE_MCP_OBSERVE_TOOLS=1 下可用（增量修改）。
""".strip()

server = MCPServer(
    name="planner-exec",
    title="Planner Exec",
    description="Plan goals with premium agent; execute phase DAGs via cheap LLM worker.",
    instructions=SERVER_INSTRUCTIONS,
    version="0.5.0",
)


# --- Core (5) ---


@server.tool(
    name="planner_plan",
    description=(
        "Create task and save goal-confirmed + phases + all phase DAGs atomically. "
        "Returns slim summary only (no full nodes[]). Use validate_only to check without writing."
    ),
)
def planner_plan(
    plan: dict[str, Any],
    task_id: str | None = None,
    workspace: str | None = None,
    agent_id: str | None = None,
    force: bool = False,
    validate_only: bool = False,
    max_node_eval_iterations: int = 3,
    max_node_execute_retries: int = 2,
) -> str:
    return _run_pe(
        pe.cmd_plan,
        argparse.Namespace(
            plan=json.dumps(plan, ensure_ascii=False),
            plan_file=None,
            task_id=task_id,
            workspace=workspace,
            agent_id=agent_id,
            force=force,
            validate_only=validate_only,
            source="mcp",
            max_node_eval_iterations=max_node_eval_iterations,
            max_node_execute_retries=max_node_execute_retries,
        ),
    )


@server.tool(
    name="planner_run",
    description="Run task (all phases) or single phase if phase is set. Default slim response.",
)
def planner_run(
    task_id: str,
    phase: int | None = None,
    from_phase: int = 1,
    skip_eval: bool = False,
    mechanical_only: bool = False,
    include_steps: bool = False,
    include_phases: bool = False,
) -> str:
    ns = argparse.Namespace(
        task_id=task_id,
        phase=phase,
        from_phase=from_phase,
        skip_eval=skip_eval,
        mechanical_only=mechanical_only,
        include_steps=include_steps,
        include_phases=include_phases,
    )
    if phase is not None:
        return _run_pe(pe.cmd_run_phase, ns)
    return _run_pe(pe.cmd_run_task, ns)


@server.tool(
    name="planner_status",
    description="Compact status with live progress, session pointers, recommended_next.",
)
def planner_status(
    task_id: str,
    last_since: str | None = None,
    last_status_line: str | None = None,
    increment_poll: bool = False,
) -> str:
    return _run_pe(
        pe.cmd_status,
        argparse.Namespace(
            task_id=task_id,
            last_since=last_since,
            last_status_line=last_status_line,
            increment_poll=increment_poll,
        ),
    )


@server.tool(
    name="planner_replan",
    description="Without patches: replan packet. With patches + phase: apply DAG patches.",
)
def planner_replan(
    task_id: str,
    phase: int | None = None,
    patches: list[dict[str, Any]] | None = None,
) -> str:
    data = json.dumps({"patches": patches}, ensure_ascii=False) if patches else None
    return _run_pe(
        pe.cmd_replan,
        argparse.Namespace(
            task_id=task_id,
            phase=phase,
            data=data,
            data_file=None,
            patches=patches,
        ),
    )


@server.tool(name="planner_query_logs", description="Task logs. Default limit=20; avoid detail=true.")
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


# --- Observe tier ---


def _register_observe_tools() -> None:
    @server.tool(name="planner_init", description="[Legacy] Create empty task. Prefer planner_plan.")
    def planner_init(
        goal: str,
        context: dict[str, Any] | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        workspace: str | None = None,
        max_node_eval_iterations: int = 3,
        max_node_execute_retries: int = 2,
    ) -> str:
        return _run_pe(
            pe.cmd_init,
            argparse.Namespace(
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
            ),
        )

    @server.tool(name="planner_save", description="[Legacy] Incremental save. Prefer planner_plan for new tasks.")
    def planner_save(
        task_id: str,
        artifact_type: str,
        data: dict[str, Any],
        phase: int | None = None,
    ) -> str:
        return _run_pe(
            pe.cmd_save,
            argparse.Namespace(
                task_id=task_id,
                type=artifact_type,
                data=json.dumps(data, ensure_ascii=False),
                data_file=None,
                phase=phase,
            ),
        )

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

    @server.tool(name="planner_progress", description="Detailed progress summary with next_action.")
    def planner_progress(task_id: str) -> str:
        return _run_pe(pe.cmd_progress, argparse.Namespace(task_id=task_id))

    @server.tool(name="planner_migrate", description="Import legacy JSON tasks into SQLite.")
    def planner_migrate(task_id: str | None = None, import_all: bool = False) -> str:
        return _run_pe(pe.cmd_migrate, argparse.Namespace(task_id=task_id, all=import_all))

    @server.tool(name="planner_token_report", description="MCP vs internal LLM token usage report.")
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

    @server.tool(name="planner_run_task", description="[Legacy] Use planner_run without phase.")
    def planner_run_task(
        task_id: str,
        from_phase: int = 1,
        skip_eval: bool = False,
        mechanical_only: bool = False,
        include_phases: bool = False,
    ) -> str:
        return planner_run(
            task_id,
            phase=None,
            from_phase=from_phase,
            skip_eval=skip_eval,
            mechanical_only=mechanical_only,
            include_phases=include_phases,
        )

    @server.tool(name="planner_run_phase", description="[Legacy] Use planner_run(phase=N).")
    def planner_run_phase(
        task_id: str,
        phase: int,
        skip_eval: bool = False,
        mechanical_only: bool = False,
        include_steps: bool = False,
    ) -> str:
        return planner_run(
            task_id,
            phase=phase,
            skip_eval=skip_eval,
            mechanical_only=mechanical_only,
            include_steps=include_steps,
        )

    @server.tool(name="planner_replan_packet", description="[Legacy] Use planner_replan without patches.")
    def planner_replan_packet(task_id: str) -> str:
        return planner_replan(task_id)

    @server.tool(name="planner_patch_node", description="[Legacy] Use planner_replan with patches.")
    def planner_patch_node(
        task_id: str,
        phase: int,
        patches: list[dict[str, Any]],
    ) -> str:
        return planner_replan(task_id, phase=phase, patches=patches)

    @server.tool(name="planner_session_get", description="[Legacy] Use planner_status.")
    def planner_session_get(task_id: str) -> str:
        return planner_status(task_id)

    @server.tool(name="planner_session_set", description="[Legacy] Use planner_status with session params.")
    def planner_session_set(
        task_id: str,
        last_since: str | None = None,
        last_status_line: str | None = None,
        increment_poll: bool = False,
    ) -> str:
        return planner_status(
            task_id,
            last_since=last_since,
            last_status_line=last_status_line,
            increment_poll=increment_poll,
        )


def _register_debug_tools() -> None:
    @server.tool(name="planner_eval_node", description="Evaluate a single DAG node. Debug only.")
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

    @server.tool(name="planner_eval_phase", description="Evaluate all nodes in a phase. Debug only.")
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

    @server.tool(name="planner_eval_status", description="Summarize node eval status. Debug only.")
    def planner_eval_status(task_id: str, phase: int) -> str:
        return _run_pe(pe.cmd_eval_status, argparse.Namespace(task_id=task_id, phase=phase))

    @server.tool(name="planner_execute_node", description="Execute one DAG node. Debug only.")
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

    @server.tool(name="planner_next_node", description="Show next executable node. Debug only.")
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
