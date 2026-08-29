"""CLI commands and argparse entrypoints."""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import db
from .pe_dag import dag_revision, next_executable_node
from .pe_llm import evaluate_node_with_llm, llm_available
from .pe_orchestrate import (
    compute_progress,
    internal_eval_node,
    internal_execute_node,
    internal_run_phase,
    eval_phase_internal,
)
from .pe_budget import emit_json_response, slim_phase_result, summarize_task_run
from .pe_patch import PatchError, apply_node_patches
from .pe_replan import build_replan_packet
from .pe_session import get_session_view, set_session_view
from .pe_token import get_token_report
from .pe_plan import PlanError, apply_plan
from .pe_paths import legacy_tasks_dir
from .pe_progress import build_live_status, emit_progress
from .pe_util import (
    DEFAULT_MAX_NODE_EVAL_ITERATIONS,
    DEFAULT_MAX_NODE_EXECUTE_RETRIES,
    VALID_SAVE_TYPES,
    ensure_task,
    make_task_id,
    read_data_arg,
    require_task_id,
    utc_now,
    validate_dag,
    validate_goal_confirmed,
    validate_phases,
)
from .pe_validate import build_node_eval_context, validate_node_mechanical


def cmd_plan(args: argparse.Namespace) -> None:
    plan = read_data_arg(getattr(args, "plan_file", None), getattr(args, "plan", None))
    if not isinstance(plan, dict):
        raise SystemExit("ERROR: plan must be a JSON object")
    try:
        result = apply_plan(
            plan,
            task_id=args.task_id,
            workspace=args.workspace,
            agent_id=args.agent_id,
            force=args.force,
            validate_only=args.validate_only,
            max_node_eval_iterations=args.max_node_eval_iterations,
            max_node_execute_retries=args.max_node_execute_retries,
            source=args.source or "mcp",
        )
    except PlanError as exc:
        raise SystemExit(f"ERROR [{exc.status}]: {exc}") from exc
    emit_json_response(
        result,
        budget=True,
        ledger={"task_id": result.get("task_id", args.task_id or ""), "tool": "planner_plan"},
    )


def cmd_init(args: argparse.Namespace) -> None:
    goal = args.goal.strip()
    if not goal:
        raise SystemExit("ERROR: --goal is required")

    context = read_data_arg(args.context_file, args.context)
    task_id = args.task_id or make_task_id(goal)
    if db.task_exists(task_id) and not args.force:
        raise SystemExit(f"ERROR: task already exists: {task_id} (use --force to overwrite)")

    now = utc_now()
    meta = {
        "task_id": task_id,
        "created_at": now,
        "updated_at": now,
        "status": "initialized",
        "max_node_eval_iterations": args.max_node_eval_iterations,
        "max_node_execute_retries": args.max_node_execute_retries,
        "agent_id": args.agent_id,
        "workspace": args.workspace,
    }
    raw_goal = {
        "goal": goal,
        "context": context,
        "captured_at": now,
        "source": args.source or "user",
    }

    if args.force and db.task_exists(task_id):
        db.delete_task(task_id)

    db.create_task(meta, raw_goal)

    print(
        json.dumps(
            {
                "task_id": task_id,
                "db_path": str(db.DB_PATH),
                "note": "always pass --task-id in subsequent commands",
            },
            ensure_ascii=False,
        )
    )


def cmd_save(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)

    data = read_data_arg(args.data_file, args.data)
    now = utc_now()
    updates: dict[str, Any] = {"updated_at": now}

    if args.type == "goal-confirmed":
        validate_goal_confirmed(data)
        db.save_artifact(task_id, "goal-confirmed", data, now)
        updates["status"] = "goal_confirmed"
        updates["goal"] = data.get("goal")

    elif args.type == "phases":
        validate_phases(data)
        db.save_artifact(task_id, "phases", data, now)
        updates["status"] = "phases_defined"
        updates["phase_count"] = len(data["phases"])

    elif args.type == "dag":
        if args.phase is None:
            raise SystemExit("ERROR: --phase is required for dag")
        validate_dag(data)
        rev = dag_revision(data)
        payload = {**data, "saved_at": now, "phase": args.phase, "dag_revision": rev}
        db.save_phase_dag(task_id, args.phase, rev, payload, now)
        updates["status"] = f"dag_defined_phase_{args.phase:02d}"
        updates["dag_revision"] = rev

    elif args.type == "execution":
        if args.phase is None:
            raise SystemExit("ERROR: --phase is required for execution")
        entry = {**data, "saved_at": now, "phase": args.phase}
        db.append_execution(task_id, args.phase, entry)
        updates["status"] = f"executing_phase_{args.phase:02d}"

    elif args.type == "status":
        db.append_status_snapshot(task_id, data, now)
        if "status" in data:
            updates["status"] = data["status"]

    else:
        raise SystemExit(f"ERROR: unknown type: {args.type}")

    db.update_task_meta(task_id, **updates)
    print(json.dumps({"ok": True, "task_id": task_id, "type": args.type}, ensure_ascii=False))


def cmd_show(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    meta = db.get_task_meta(task_id)
    progress = compute_progress(task_id)
    print(
        json.dumps(
            {
                "task_id": task_id,
                "db_path": str(db.DB_PATH),
                "meta": meta,
                "progress": progress,
                "llm_available": llm_available(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_progress(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    print(json.dumps(compute_progress(task_id), ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    db.init_db()
    tasks = []
    for meta in db.list_tasks():
        task_id = meta["task_id"]
        progress = compute_progress(task_id)
        goal = progress.get("goal") or ""
        if args.query and args.query.lower() not in goal.lower() and args.query not in task_id:
            continue
        if args.status and progress.get("status") != args.status and meta.get("status") != args.status:
            continue
        if args.workspace and meta.get("workspace") != args.workspace:
            continue
        if args.agent_id and meta.get("agent_id") != args.agent_id:
            continue
        tasks.append(
            {
                "task_id": task_id,
                "status": meta.get("status"),
                "goal": goal[:120],
                "updated_at": meta.get("updated_at"),
                "next_action": progress.get("next_action"),
                "agent_id": meta.get("agent_id"),
                "workspace": meta.get("workspace"),
            }
        )
    print(json.dumps(tasks, ensure_ascii=False, indent=2))


def cmd_eval_node(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    if not args.node:
        raise SystemExit("ERROR: --node is required")

    ensure_task(task_id)
    meta = db.get_task_meta(task_id)
    max_iter = int(meta.get("max_node_eval_iterations", DEFAULT_MAX_NODE_EVAL_ITERATIONS))

    dag = db.get_phase_dag(task_id, args.phase)
    if not dag:
        raise SystemExit(f"ERROR: dag not found for phase {args.phase}")

    nodes = dag.get("nodes", [])
    nodes_by_id = {n["id"]: n for n in nodes}
    node = nodes_by_id.get(args.node)
    if not node:
        raise SystemExit(f"ERROR: node not found in dag: {args.node}")

    rev = dag.get("dag_revision")
    latest = db.latest_node_eval(task_id, args.phase, args.node, rev)
    if args.iteration is not None:
        iteration = args.iteration
    else:
        iteration = int((latest or {}).get("iteration", 0)) + 1

    if iteration > max_iter and not args.force:
        raise SystemExit(
            f"ERROR: node {args.node} exceeded max eval iterations ({max_iter}); use --force to continue"
        )

    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phase_def = None
    if 0 < args.phase <= len(phases_doc.get("phases", [])):
        phase_def = phases_doc["phases"][args.phase - 1]
    goal = db.get_artifact(task_id, "goal-confirmed")

    mechanical = validate_node_mechanical(node, nodes_by_id, phase_def)

    context = build_node_eval_context(node, nodes_by_id, dag, phase_def, goal)
    context["task_id"] = task_id
    context["phase_number"] = args.phase
    context["workspace"] = db.get_task_meta(task_id).get("workspace")
    llm_result = evaluate_node_with_llm(context)

    combined_issues = list(mechanical.get("issues", []))
    llm_passed: bool | None = llm_result.get("passed") if not llm_result.get("skipped") else None

    if llm_passed is False:
        for issue in llm_result.get("issues", []):
            combined_issues.append(
                {
                    "node_id": args.node,
                    "severity": issue.get("severity", "blocker"),
                    "type": issue.get("type", "ambiguity"),
                    "message": issue.get("message", ""),
                    "source": "llm",
                }
            )

    blocker_count = sum(1 for i in combined_issues if i.get("severity") == "blocker")
    if not mechanical.get("passed"):
        passed = False
    elif llm_result.get("skipped"):
        passed = False
        combined_issues.append(
            {
                "node_id": args.node,
                "severity": "blocker",
                "type": "llm",
                "message": llm_result.get("reason", "LLM evaluation was skipped"),
                "source": "system",
            }
        )
        blocker_count += 1
    elif llm_passed is True:
        passed = True
    else:
        passed = False

    result = {
        "task_id": task_id,
        "phase": args.phase,
        "node_id": args.node,
        "dag_revision": rev,
        "iteration": iteration,
        "passed": passed,
        "mechanical": mechanical,
        "llm": llm_result,
        "issues": combined_issues,
        "blocker_count": blocker_count,
        "evaluated_at": utc_now(),
        "next_step": (
            "node ready for execution"
            if passed
            else "revise this node in dag, save dag, then re-run eval-node"
        ),
    }

    db.save_node_eval(result)
    db.update_task_meta(
        task_id,
        updated_at=utc_now(),
        status=f"node_eval_phase_{args.phase:02d}_{args.node}_iter_{iteration:02d}",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_eval_status(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")

    ensure_task(task_id)
    dag = db.get_phase_dag(task_id, args.phase)
    if not dag:
        raise SystemExit(f"ERROR: dag not found for phase {args.phase}")

    rev = dag.get("dag_revision")
    nodes = dag.get("nodes", [])

    rows = []
    all_passed = True
    for node in nodes:
        nid = node["id"]
        latest = db.latest_node_eval(task_id, args.phase, nid, rev)
        passed = bool(latest and latest.get("passed") is True)
        if not passed:
            all_passed = False
        rows.append(
            {
                "node_id": nid,
                "title": node.get("title"),
                "latest_iteration": (latest or {}).get("iteration"),
                "passed": passed,
                "blocker_count": (latest or {}).get("blocker_count", 0),
            }
        )

    print(
        json.dumps(
            {
                "task_id": task_id,
                "phase": args.phase,
                "all_nodes_passed": all_passed,
                "ready_for_execution": all_passed,
                "nodes": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )




def cmd_execute_node(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    ensure_task(task_id)
    result = internal_execute_node(task_id, args.phase, args.node, dry_run=args.dry_run)
    if result.get("node_id"):
        db.update_task_meta(
            task_id,
            updated_at=utc_now(),
            status=f"executing_phase_{args.phase:02d}_{result['node_id']}",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_next_node(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    ensure_task(task_id)
    dag = db.get_phase_dag(task_id, args.phase)
    if not dag:
        raise SystemExit(f"ERROR: dag not found for phase {args.phase}")
    executions = db.load_executions(task_id, args.phase)
    meta = db.get_task_meta(task_id)
    max_retries = int(meta.get("max_node_execute_retries", DEFAULT_MAX_NODE_EXECUTE_RETRIES))
    nxt = next_executable_node(dag, executions, max_retries=max_retries)
    rev = dag.get("dag_revision")

    payload: dict[str, Any] = {"task_id": task_id, "phase": args.phase, "next": None}
    if nxt:
        nid = nxt["node"]["id"]
        latest_eval = db.latest_node_eval(task_id, args.phase, nid, rev)
        payload["next"] = {
            "node_id": nid,
            "action": nxt["action"],
            "reason": nxt.get("reason"),
            "eval_passed": bool(latest_eval and latest_eval.get("passed")),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_eval_phase(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    ensure_task(task_id)
    dag = db.get_phase_dag(task_id, args.phase)
    if not dag:
        raise SystemExit(f"ERROR: dag not found for phase {args.phase}")
    rev = dag.get("dag_revision")
    results = []
    for node in dag.get("nodes", []):
        nid = node["id"]
        if args.node and nid != args.node:
            continue
        latest = db.latest_node_eval(task_id, args.phase, nid, rev)
        if latest and latest.get("passed") and not args.force:
            results.append({"node_id": nid, "passed": True, "cached": True})
            continue
        results.append(
            internal_eval_node(task_id, args.phase, nid, force=args.force)
        )
    all_passed = all(r.get("passed") for r in results)
    print(json.dumps({"task_id": task_id, "phase": args.phase, "all_passed": all_passed, "nodes": results}, ensure_ascii=False, indent=2))




def cmd_run(args: argparse.Namespace) -> None:
    """Unified run: single phase if --phase set, else full task."""
    if args.phase is not None:
        cmd_run_phase(args)
        return
    cmd_run_task(args)


def cmd_run_phase(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    ensure_task(task_id)
    result = internal_run_phase(
        task_id,
        args.phase,
    )
    payload = slim_phase_result(result, include_steps=args.include_steps)
    emit_json_response(
        payload,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_run"},
    )


def cmd_run_task(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phases = phases_doc.get("phases", [])
    if not phases:
        raise SystemExit("ERROR: no phases saved for task")

    results: list[dict[str, Any]] = []
    emit_progress(task_id, "task_start", status="running", message=f"from_phase={args.from_phase or 1}")
    for idx in range(1, len(phases) + 1):
        if args.from_phase and idx < args.from_phase:
            continue
        result = internal_run_phase(
            task_id,
            idx,
        )
        results.append({"phase": idx, **result})
        if result.get("status") != "completed":
            break

    final_status, blocked = summarize_task_run(results)
    emit_progress(task_id, "task_done", status=final_status)
    live = build_live_status(task_id, compute_progress(task_id))
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": final_status,
        "live": live,
    }
    if blocked:
        payload["blocked"] = blocked
    if args.include_phases:
        payload["phases"] = results
    emit_json_response(
        payload,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_run"},
    )


def cmd_status(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    if getattr(args, "last_since", None) or getattr(args, "last_status_line", None) or getattr(
        args, "increment_poll", False
    ):
        view = set_session_view(
            task_id,
            last_since=getattr(args, "last_since", None),
            last_status_line=getattr(args, "last_status_line", None),
            increment_poll=getattr(args, "increment_poll", False),
        )
    else:
        view = get_session_view(task_id)
    emit_json_response(
        view,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_status"},
    )


def cmd_migrate(args: argparse.Namespace) -> None:
    legacy_root = legacy_tasks_dir()
    if args.task_id:
        path = legacy_root / args.task_id
        if not path.exists():
            raise SystemExit(f"ERROR: legacy task dir not found: {path}")
        imported = [db.migrate_json_task(path, args.task_id)]
    else:
        imported = db.migrate_all_json_tasks(legacy_root if args.all else None)
    print(json.dumps({"imported": imported, "db_path": str(db.DB_PATH)}, ensure_ascii=False, indent=2))


def cmd_query_logs(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    log_types = None
    if args.log_types:
        log_types = [t.strip() for t in args.log_types.split(",") if t.strip()]
    result = db.query_task_logs(
        task_id=task_id,
        phase=args.phase,
        node_id=args.node,
        log_types=log_types,
        limit=args.limit,
        detail=args.detail,
        since=args.since,
        offset=args.offset,
        failures_only=args.failures_only,
    )
    emit_json_response(
        result,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_query_logs"},
    )


def cmd_replan(args: argparse.Namespace) -> None:
    """Unified replan: return packet if no patches; apply patches if provided."""
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    patches = getattr(args, "patches", None)
    if patches:
        if args.phase is None:
            raise SystemExit("ERROR: --phase is required when applying patches")
        if not isinstance(patches, list) or not patches:
            raise SystemExit("ERROR: expected non-empty patches array")
        try:
            result = apply_node_patches(task_id, args.phase, patches)
        except PatchError as exc:
            raise SystemExit(f"ERROR [{exc.status}]: {exc}") from exc
        emit_json_response(
            result,
            budget=True,
            ledger={"task_id": task_id, "tool": "planner_replan"},
        )
        return
    packet = build_replan_packet(task_id)
    emit_json_response(
        packet,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_replan"},
    )


def cmd_replan_packet(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    packet = build_replan_packet(task_id)
    emit_json_response(
        packet,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_replan_packet"},
    )


def cmd_patch_node(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    if args.phase is None:
        raise SystemExit("ERROR: --phase is required")
    ensure_task(task_id)
    data = read_data_arg(args.data_file, args.data)
    patches = data.get("patches") if isinstance(data, dict) else data
    if not isinstance(patches, list) or not patches:
        raise SystemExit("ERROR: expected non-empty patches array")
    try:
        result = apply_node_patches(task_id, args.phase, patches)
    except PatchError as exc:
        raise SystemExit(f"ERROR [{exc.status}]: {exc}") from exc
    emit_json_response(
        result,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_patch_node"},
    )


def cmd_token_report(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    result = get_token_report(
        task_id,
        host_input_tokens=args.host_input_tokens,
        host_output_tokens=args.host_output_tokens,
    )
    emit_json_response(
        result,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_token_report"},
    )


def cmd_session_get(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    view = get_session_view(task_id)
    emit_json_response(
        view,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_session_get"},
    )


def cmd_session_set(args: argparse.Namespace) -> None:
    task_id = require_task_id(args.task_id)
    ensure_task(task_id)
    view = set_session_view(
        task_id,
        last_since=args.last_since,
        last_status_line=args.last_status_line,
        increment_poll=args.increment_poll,
    )
    emit_json_response(
        view,
        budget=True,
        ledger={"task_id": task_id, "tool": "planner_session_set"},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Planner-Exec task CLI (multi-agent safe)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Atomic init + goal/phases/dags (replaces init+save×N)")
    p_plan.add_argument("--plan-file", help="JSON plan file")
    p_plan.add_argument("--plan", help="Inline JSON plan string")
    p_plan.add_argument("--task-id", help="Explicit task id")
    p_plan.add_argument("--workspace", help="Workspace path for execution")
    p_plan.add_argument("--agent-id", help="Optional agent identifier")
    p_plan.add_argument("--force", action="store_true", help="Overwrite existing task")
    p_plan.add_argument("--validate-only", action="store_true", help="Validate plan without writing")
    p_plan.add_argument("--source", default="cli", help="Plan source label")
    p_plan.add_argument(
        "--max-node-eval-iterations",
        type=int,
        default=DEFAULT_MAX_NODE_EVAL_ITERATIONS,
    )
    p_plan.add_argument(
        "--max-node-execute-retries",
        type=int,
        default=DEFAULT_MAX_NODE_EXECUTE_RETRIES,
    )
    p_plan.set_defaults(func=cmd_plan)

    p_init = sub.add_parser("init", help="[legacy] Use plan — create task metadata only")
    p_init.add_argument("--goal", required=True, help="User goal text")
    p_init.add_argument("--context-file", help="JSON file with goal context")
    p_init.add_argument("--context", help="Inline JSON context string")
    p_init.add_argument("--task-id", help="Explicit task id (default: YYMMDD-slug)")
    p_init.add_argument("--source", default="user", help="Goal source label")
    p_init.add_argument("--agent-id", help="Optional agent identifier for list/filter")
    p_init.add_argument("--workspace", help="Optional workspace path for list/filter")
    p_init.add_argument("--force", action="store_true", help="Allow re-init metadata")
    p_init.add_argument(
        "--max-node-eval-iterations",
        type=int,
        default=DEFAULT_MAX_NODE_EVAL_ITERATIONS,
        help="Max eval iterations per DAG node",
    )
    p_init.add_argument(
        "--max-node-execute-retries",
        type=int,
        default=DEFAULT_MAX_NODE_EXECUTE_RETRIES,
        help="Max execute retries per node before escalation",
    )
    p_init.set_defaults(func=cmd_init)

    p_save = sub.add_parser("save", help="[legacy] Incremental artifact save; prefer plan for new tasks")
    p_save.add_argument("--type", required=True, choices=sorted(VALID_SAVE_TYPES))
    p_save.add_argument("--task-id", required=True, help="Task id")
    p_save.add_argument("--phase", type=int, help="Phase number for dag/execution")
    p_save.add_argument("--data-file", help="JSON payload file")
    p_save.add_argument("--data", help="Inline JSON payload")
    p_save.set_defaults(func=cmd_save)

    p_show = sub.add_parser("show", help="Show task metadata and progress")
    p_show.add_argument("--task-id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_progress = sub.add_parser("progress", help="Show progress summary")
    p_progress.add_argument("--task-id", required=True)
    p_progress.set_defaults(func=cmd_progress)

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--query", help="Filter by goal substring or task id")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--workspace", help="Filter by workspace")
    p_list.add_argument("--agent-id", help="Filter by agent id")
    p_list.set_defaults(func=cmd_list)

    p_eval = sub.add_parser("eval-node", help="Evaluate a single DAG node (mechanical + LLM)")
    p_eval.add_argument("--task-id", required=True)
    p_eval.add_argument("--phase", type=int, required=True)
    p_eval.add_argument("--node", required=True, help="DAG node id")
    p_eval.add_argument("--iteration", type=int, help="Explicit iteration number")
    p_eval.add_argument("--force", action="store_true", help="Allow eval beyond max iterations")
    p_eval.set_defaults(func=cmd_eval_node)

    p_eval_status = sub.add_parser("eval-status", help="Summarize node eval status for a phase")
    p_eval_status.add_argument("--task-id", required=True)
    p_eval_status.add_argument("--phase", type=int, required=True)
    p_eval_status.set_defaults(func=cmd_eval_status)

    p_eval_phase = sub.add_parser("eval-phase", help="Evaluate all unevaluated nodes in a phase (cheap LLM)")
    p_eval_phase.add_argument("--task-id", required=True)
    p_eval_phase.add_argument("--phase", type=int, required=True)
    p_eval_phase.add_argument("--node", help="Evaluate only one node")
    p_eval_phase.add_argument("--force", action="store_true", help="Re-eval even if passed")
    p_eval_phase.set_defaults(func=cmd_eval_phase)

    p_execute = sub.add_parser("execute-node", help="Execute next/specified node via cheap LLM worker")
    p_execute.add_argument("--task-id", required=True)
    p_execute.add_argument("--phase", type=int, required=True)
    p_execute.add_argument("--node", help="Expected node id (must match next)")
    p_execute.add_argument("--dry-run", action="store_true")
    p_execute.set_defaults(func=cmd_execute_node)

    p_next = sub.add_parser("next-node", help="Show next executable node without running")
    p_next.add_argument("--task-id", required=True)
    p_next.add_argument("--phase", type=int, required=True)
    p_next.set_defaults(func=cmd_next_node)

    p_run_unified = sub.add_parser("run", help="Run task (all phases) or single phase if --phase set")
    p_run_unified.add_argument("--task-id", required=True)
    p_run_unified.add_argument("--phase", type=int, help="If set, run only this phase")
    p_run_unified.add_argument("--from-phase", type=int, default=1, help="Start from phase N (task run only)")
    p_run_unified.add_argument("--include-steps", action="store_true", help="Include steps[] (single phase)")
    p_run_unified.add_argument("--include-phases", action="store_true", help="Include phases[] (full task)")
    p_run_unified.set_defaults(func=cmd_run)

    p_run = sub.add_parser("run-phase", help="[legacy] Use run --phase N")
    p_run.add_argument("--task-id", required=True)
    p_run.add_argument("--phase", type=int, required=True)
    p_run.add_argument(
        "--include-steps",
        action="store_true",
        help="Include full steps[] in response (deprecated; use query-logs instead)",
    )
    p_run.set_defaults(func=cmd_run_phase)

    p_run_task = sub.add_parser("run-task", help="[legacy] Use run without --phase")
    p_run_task.add_argument("--task-id", required=True)
    p_run_task.add_argument("--from-phase", type=int, default=1, help="Start from phase N")
    p_run_task.add_argument(
        "--include-phases",
        action="store_true",
        help="Include full phases[] in response (deprecated; use query-logs instead)",
    )
    p_run_task.set_defaults(func=cmd_run_task)

    p_status = sub.add_parser("status", help="Status + session pointers + recommended_next")
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument("--last-since", help="Update session: ISO timestamp for query_logs since=")
    p_status.add_argument("--last-status-line", help="Update session: compact status line")
    p_status.add_argument("--increment-poll", action="store_true", help="Update session: bump poll_count")
    p_status.set_defaults(func=cmd_status)

    p_migrate = sub.add_parser("migrate", help="Import legacy JSON tasks into SQLite")
    p_migrate.add_argument("--task-id", help="Import one legacy task dir by id")
    p_migrate.add_argument("--all", action="store_true", help="Import all under ~/.planer-exec/tasks/")
    p_migrate.set_defaults(func=cmd_migrate)

    p_logs = sub.add_parser("query-logs", help="Query task execution timeline for replanning")
    p_logs.add_argument("--task-id", required=True)
    p_logs.add_argument("--phase", type=int)
    p_logs.add_argument("--node", help="Filter by node id")
    p_logs.add_argument(
        "--log-types",
        help="Comma-separated: eval,execution,escalation,status,progress,agent_trace",
    )
    p_logs.add_argument("--limit", type=int, default=20)
    p_logs.add_argument("--detail", action="store_true", help="Include full record payloads")
    p_logs.add_argument("--since", help="Only entries after this ISO timestamp (for polling)")
    p_logs.add_argument("--offset", type=int, default=0, help="Skip first N matched entries")
    p_logs.add_argument(
        "--failures-only",
        action="store_true",
        help="Only failed executions, escalations, and failed tool traces",
    )
    p_logs.set_defaults(func=cmd_query_logs)

    p_replan_unified = sub.add_parser("replan", help="Replan packet (no patches) or apply patches")
    p_replan_unified.add_argument("--task-id", required=True)
    p_replan_unified.add_argument("--phase", type=int, help="Required when applying patches")
    p_replan_unified.add_argument("--data-file", help="JSON with patches array")
    p_replan_unified.add_argument("--data", help="Inline JSON with patches array")
    p_replan_unified.set_defaults(func=cmd_replan)

    p_replan = sub.add_parser("replan-packet", help="[legacy] Use replan without patches")
    p_replan.add_argument("--task-id", required=True)
    p_replan.set_defaults(func=cmd_replan_packet)

    p_patch = sub.add_parser("patch-node", help="[legacy] Use replan --phase N --data '{patches:[...]}'")
    p_patch.add_argument("--task-id", required=True)
    p_patch.add_argument("--phase", type=int, required=True)
    p_patch.add_argument("--data-file", help="JSON with patches array")
    p_patch.add_argument("--data", help="Inline JSON with patches array")
    p_patch.set_defaults(func=cmd_patch_node)

    p_token = sub.add_parser("token-report", help="Token/char usage report for a task")
    p_token.add_argument("--task-id", required=True)
    p_token.add_argument("--host-input-tokens", type=int, help="Optional host-reported input tokens")
    p_token.add_argument("--host-output-tokens", type=int, help="Optional host-reported output tokens")
    p_token.set_defaults(func=cmd_token_report)

    p_sess_get = sub.add_parser("session-get", help="[legacy] Use status (session included)")
    p_sess_get.add_argument("--task-id", required=True)
    p_sess_get.set_defaults(func=cmd_session_get)

    p_sess_set = sub.add_parser("session-set", help="[legacy] Use status --last-since ... --increment-poll")
    p_sess_set.add_argument("--task-id", required=True)
    p_sess_set.add_argument("--last-since", help="ISO timestamp for query_logs since=")
    p_sess_set.add_argument("--last-status-line", help="Compact status line to remember")
    p_sess_set.add_argument("--increment-poll", action="store_true", help="Bump poll_count by 1")
    p_sess_set.set_defaults(func=cmd_session_set)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
