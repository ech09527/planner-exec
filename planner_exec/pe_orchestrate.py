"""DAG eval/execute orchestration (internal runtime)."""

from __future__ import annotations

from typing import Any

from . import db
from .pe_acceptance import check_node_acceptance
from .pe_actions import apply_actions
from .pe_agent import classify_agent_failure
from .pe_dag import (
    next_executable_node,
    node_failed_count,
    phase_execution_complete,
    resolve_node_inputs,
)
from .pe_dag_eval import ensure_dag_eval_passed
from .pe_llm import evaluate_node_with_llm, execute_node_with_llm
from .pe_node import node_dependencies, slim_node_for_prompt
from .pe_progress import emit_progress
from .pe_util import DEFAULT_MAX_NODE_EVAL_ITERATIONS, DEFAULT_MAX_NODE_EXECUTE_RETRIES, utc_now
from .pe_validate import build_node_eval_context, validate_node_mechanical

def compute_progress(task_id: str) -> dict[str, Any]:
    meta = db.get_task_meta(task_id)
    raw_goal = db.get_artifact(task_id, "goal-raw")
    goal = db.get_artifact(task_id, "goal-confirmed")
    phases_doc = db.get_artifact(task_id, "phases")

    progress: dict[str, Any] = {
        "task_id": meta.get("task_id", task_id),
        "status": meta.get("status"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "goal": (goal or raw_goal or {}).get("goal"),
        "steps": {
            "initialized": raw_goal is not None,
            "goal_confirmed": goal is not None and not (goal or {}).get("open_questions"),
            "phases_defined": phases_doc is not None,
        },
        "phases": [],
        "next_action": None,
    }

    if not progress["steps"]["initialized"]:
        progress["next_action"] = "run init"
        return progress
    if not progress["steps"]["goal_confirmed"]:
        progress["next_action"] = "confirm goal and save goal-confirmed"
        return progress
    if not progress["steps"]["phases_defined"]:
        progress["next_action"] = "split phases and save phases"
        return progress

    phases = phases_doc["phases"]
    for idx, phase in enumerate(phases, start=1):
        dag = db.get_phase_dag(task_id, idx)
        executions = db.load_executions(task_id, idx)

        node_summaries = []
        all_nodes_passed = True
        required_nodes: set[str] = set()
        if dag:
            nodes = dag.get("nodes", [])
            rev = dag.get("dag_revision")
            required_nodes = {n["id"] for n in nodes}

            for node in nodes:
                nid = node["id"]
                latest_eval = db.latest_node_eval(task_id, idx, nid, rev)
                node_execs = [e for e in executions if e.get("node_id") == nid]
                last_exec = node_execs[-1] if node_execs else None
                eval_passed = bool(latest_eval and latest_eval.get("passed") is True)
                if nid in required_nodes and not eval_passed:
                    all_nodes_passed = False
                node_summaries.append(
                    {
                        "node_id": nid,
                        "title": node.get("title"),
                        "eval_passed": eval_passed,
                        "eval_iteration": (latest_eval or {}).get("iteration"),
                        "execution_status": (last_exec or {}).get("status"),
                        "required": nid in required_nodes,
                    }
                )
        else:
            all_nodes_passed = False

        executed_ok = {
            e.get("node_id")
            for e in executions
            if e.get("status") in ("success", "skipped") and e.get("node_id")
        }
        execution_complete = phase_execution_complete(dag, executions) if dag else False

        phase_info = {
            "phase_index": idx,
            "phase_id": phase.get("id"),
            "title": phase.get("title"),
            "dag_saved": dag is not None,
            "nodes": node_summaries,
            "all_nodes_eval_passed": all_nodes_passed if dag else False,
            "execution_complete": execution_complete,
        }
        progress["phases"].append(phase_info)

        if progress["next_action"] is None:
            if dag is None:
                progress["next_action"] = f"phase {idx}: generate and save dag"
            elif not all_nodes_passed:
                pending = [n["node_id"] for n in node_summaries if not n["eval_passed"]]
                progress["next_action"] = f"phase {idx}: eval-node for {', '.join(pending)}"
            elif not execution_complete:
                pending_exec = [
                    n["node_id"]
                    for n in node_summaries
                    if n.get("required") and n.get("execution_status") not in ("success", "skipped")
                ]
                progress["next_action"] = f"phase {idx}: run-phase or execute-node for {', '.join(pending_exec)}"
            else:
                continue

    if progress["next_action"] is None:
        progress["next_action"] = "all phases complete — finalize status"

    return progress


def internal_eval_node(
    task_id: str,
    phase: int,
    node_id: str,
    force: bool = False,
) -> dict[str, Any]:
    meta = db.get_task_meta(task_id)
    max_iter = int(meta.get("max_node_eval_iterations", DEFAULT_MAX_NODE_EVAL_ITERATIONS))
    dag = db.get_phase_dag(task_id, phase)
    if not dag:
        return {"passed": False, "error": "dag not found"}
    nodes_by_id = {n["id"]: n for n in dag.get("nodes", [])}
    node = nodes_by_id[node_id]
    rev = dag.get("dag_revision")

    latest = db.latest_node_eval(task_id, phase, node_id, rev)
    iteration = int((latest or {}).get("iteration", 0)) + 1
    if iteration > max_iter and not force:
        return {"passed": False, "error": f"max eval iterations exceeded for {node_id}"}

    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phase_def = phases_doc["phases"][phase - 1] if 0 < phase <= len(phases_doc.get("phases", [])) else None
    goal = db.get_artifact(task_id, "goal-confirmed")
    mechanical = validate_node_mechanical(node, nodes_by_id, phase_def)

    context = build_node_eval_context(node, nodes_by_id, dag, phase_def, goal)
    context["task_id"] = task_id
    context["phase_number"] = phase
    meta = db.get_task_meta(task_id)
    raw_goal = db.get_artifact(task_id, "goal-raw")
    context["workspace"] = meta.get("workspace") or (raw_goal or {}).get("context", {}).get("workspace")
    llm_result = evaluate_node_with_llm(context)

    combined_issues = list(mechanical.get("issues", []))
    llm_passed = llm_result.get("passed") if not llm_result.get("skipped") else None
    if llm_passed is False:
        for issue in llm_result.get("issues", []):
            combined_issues.append(
                {
                    "node_id": node_id,
                    "severity": issue.get("severity", "blocker"),
                    "type": issue.get("type", "ambiguity"),
                    "message": issue.get("message", ""),
                    "source": "llm",
                }
            )

    if not mechanical.get("passed"):
        passed = False
    elif llm_result.get("skipped"):
        passed = False
        combined_issues.append(
            {
                "node_id": node_id,
                "severity": "blocker",
                "type": "llm",
                "message": llm_result.get("reason", "LLM skipped"),
                "source": "system",
            }
        )
    else:
        passed = llm_passed is True

    result = {
        "task_id": task_id,
        "phase": phase,
        "node_id": node_id,
        "dag_revision": rev,
        "iteration": iteration,
        "passed": passed,
        "mechanical": mechanical,
        "llm": llm_result,
        "issues": combined_issues,
        "blocker_count": sum(1 for i in combined_issues if i.get("severity") == "blocker"),
        "evaluated_at": utc_now(),
    }
    db.save_node_eval(result)
    return result


def internal_execute_node(
    task_id: str,
    phase: int,
    node_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    meta = db.get_task_meta(task_id)
    max_retries = int(meta.get("max_node_execute_retries", DEFAULT_MAX_NODE_EXECUTE_RETRIES))
    dag = db.get_phase_dag(task_id, phase)
    if not dag:
        return {"status": "failed", "message": "dag not found"}
    executions = db.load_executions(task_id, phase)
    goal = db.get_artifact(task_id, "goal-confirmed")
    raw_goal = db.get_artifact(task_id, "goal-raw")
    workspace = meta.get("workspace") or (raw_goal or {}).get("context", {}).get("workspace")

    nxt = next_executable_node(dag, executions, max_retries=max_retries)
    if not nxt:
        return {"status": "phase_complete", "message": "no pending nodes"}
    if nxt.get("action") == "blocked":
        nid = nxt["node"]["id"]
        emit_progress(
            task_id,
            "blocked",
            phase=phase,
            node_id=nid,
            status="blocked",
            message=nxt.get("reason"),
        )
        return {
            "status": "blocked",
            "node_id": nid,
            "message": nxt.get("reason") or f"node {nid} blocked",
            "escalate": True,
        }

    node = nxt["node"]
    nid = node["id"]
    if node_id and nid != node_id:
        return {
            "status": "blocked",
            "message": f"next node is {nid}, not {node_id}",
            "next_node": nid,
        }
    started_at = utc_now()

    if nxt["action"] == "skip":
        entry = {
            "node_id": nid,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "skipped",
            "inputs_used": {},
            "outputs": {},
            "acceptance_check": [nxt.get("reason") or "condition skip"],
            "executor": "script",
            "model": None,
        }
        if not dry_run:
            db.append_execution(task_id, phase, {**entry, "saved_at": utc_now(), "phase": phase})
        return {"status": "skipped", "node_id": nid, "record": entry}

    rev = dag.get("dag_revision")
    latest_eval = db.latest_node_eval(task_id, phase, nid, rev)
    if not latest_eval or not latest_eval.get("passed"):
        return {"status": "blocked", "message": f"node {nid} not eval-passed", "node_id": nid}

    inputs, input_errors = resolve_node_inputs(node, executions, goal, raw_goal)
    if input_errors:
        entry = {
            "node_id": nid,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "failed",
            "inputs_used": inputs,
            "outputs": {},
            "acceptance_check": [],
            "error": "; ".join(input_errors),
            "executor": "script",
        }
        if not dry_run:
            now = utc_now()
            db.append_execution(task_id, phase, {**entry, "saved_at": now, "phase": phase})
            db.write_escalation(
                task_id,
                phase,
                {"task_id": task_id, "node_id": nid, "reason": "input_resolution_failed", "errors": input_errors},
                now,
            )
        return {"status": "failed", "node_id": nid, "record": entry}

    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phase_def = phases_doc["phases"][phase - 1] if 0 < phase <= len(phases_doc.get("phases", [])) else None
    nodes_by_id = {n["id"]: n for n in dag.get("nodes", [])}
    upstream_nodes = {
        dep_id: slim_node_for_prompt(nodes_by_id[dep_id])
        for dep_id in node_dependencies(node)
        if dep_id in nodes_by_id
    }
    context = {
        "node": slim_node_for_prompt(node),
        "inputs": inputs,
        "workspace": workspace,
        "phase": {"id": (phase_def or {}).get("id"), "title": (phase_def or {}).get("title")},
        "task_id": task_id,
        "phase_number": phase,
        "upstream_nodes": upstream_nodes,
    }

    if dry_run:
        return {"status": "dry_run", "node_id": nid, "context": context}

    emit_progress(task_id, "node_start", phase=phase, node_id=nid, status="running")

    llm_result = execute_node_with_llm(context)
    if llm_result.get("skipped"):
        fail_reason = llm_result.get("fail_reason") or classify_agent_failure(
            llm_result.get("reason") or "llm_unavailable"
        )
        if fail_reason == "agent_error" and not llm_result.get("model"):
            fail_reason = "llm_unavailable"
        entry = {
            "node_id": nid,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "failed",
            "inputs_used": inputs,
            "outputs": {},
            "error": llm_result.get("reason"),
            "executor": llm_result.get("executor", "script"),
            "model": llm_result.get("model"),
            "fail_reason": fail_reason,
        }
        now = utc_now()
        db.append_execution(task_id, phase, {**entry, "saved_at": now, "phase": phase})
        db.write_escalation(
            task_id, phase, {"task_id": task_id, "node_id": nid, "reason": fail_reason, "detail": llm_result}, now
        )
        return {"status": "failed", "node_id": nid, "record": entry, "escalate": True}

    raw_actions = llm_result.get("actions") or []
    if raw_actions and any("ok" in a for a in raw_actions):
        action_results = raw_actions
    else:
        action_results = apply_actions(raw_actions, workspace)
    action_failed = any(not r.get("ok") for r in action_results)
    status = llm_result.get("status", "failed")
    if action_failed:
        status = "failed"

    acceptance = check_node_acceptance(node, workspace, status)
    if not acceptance.get("skipped"):
        # Defined mechanical checks are authoritative.
        if acceptance.get("passed"):
            status = "success"
            combined_error = None
        else:
            status = "failed"
            acceptance_msg = "; ".join(
                r.get("error") or f"{r.get('type')} failed" for r in acceptance.get("results", []) if not r.get("passed")
            )
            llm_error = llm_result.get("error") or ""
            combined_error = "; ".join(filter(None, [llm_error, acceptance_msg or acceptance.get("reason")]))
    elif not acceptance.get("passed"):
        status = "failed"
        acceptance_msg = "; ".join(
            r.get("error") or f"{r.get('type')} failed" for r in acceptance.get("results", []) if not r.get("passed")
        )
        llm_error = llm_result.get("error") or ""
        combined_error = "; ".join(filter(None, [llm_error, acceptance_msg or acceptance.get("reason")]))
    else:
        combined_error = llm_result.get("error")

    entry = {
        "node_id": nid,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": status,
        "inputs_used": inputs,
        "outputs": llm_result.get("outputs") or {},
        "acceptance_check": llm_result.get("acceptance_check") or [],
        "acceptance_mechanical": acceptance,
        "actions": action_results,
        "error": combined_error,
        "executor": llm_result.get("executor", "pydantic-ai"),
        "model": llm_result.get("model"),
        "fail_reason": llm_result.get("fail_reason"),
    }
    now = utc_now()
    db.append_execution(task_id, phase, {**entry, "saved_at": now, "phase": phase})

    emit_progress(
        task_id,
        "node_done",
        phase=phase,
        node_id=nid,
        status=status,
        message=combined_error,
    )

    if status != "success":
        retries = node_failed_count(executions, nid)
        escalate = retries + 1 >= max_retries
        if escalate:
            db.write_escalation(
                task_id,
                phase,
                {
                    "task_id": task_id,
                    "node_id": nid,
                    "reason": "max_execute_retries",
                    "last_record": entry,
                    "hint": "premium agent should replan this node or adjust DAG",
                },
                now,
            )
        return {"status": status, "node_id": nid, "record": entry, "escalate": escalate}

    return {"status": status, "node_id": nid, "record": entry}


def internal_run_phase(
    task_id: str,
    phase: int,
) -> dict[str, Any]:
    emit_progress(task_id, "phase_start", phase=phase, status="running")

    dag_gate = ensure_dag_eval_passed(task_id)
    if not dag_gate.get("passed"):
        emit_progress(
            task_id,
            "phase_blocked",
            phase=phase,
            status="dag_eval_failed",
            message="whole-DAG eval did not pass",
        )
        return {
            "status": "blocked",
            "stage": "dag_eval",
            "task_id": task_id,
            "phase": phase,
            "dag_eval": {
                "passed": False,
                "cached": dag_gate.get("cached"),
                "blocker_count": dag_gate.get("blocker_count"),
                "issues": (dag_gate.get("issues") or [])[:8],
                "suggestions": (dag_gate.get("suggestions") or [])[:5],
            },
            "message": "dag_eval failed — Host should revise plan then replan/plan",
        }

    eval_result = eval_phase_internal(task_id, phase)
    if not eval_result["all_passed"]:
        emit_progress(
            task_id,
            "phase_blocked",
            phase=phase,
            status="eval_failed",
            message="node eval did not pass",
        )
        return {"status": "blocked", "stage": "eval", "task_id": task_id, "phase": phase, **eval_result}
    emit_progress(task_id, "phase_eval_done", phase=phase, status="passed")

    steps: list[dict[str, Any]] = []
    while True:
        result = internal_execute_node(task_id, phase)
        if result.get("status") == "phase_complete":
            break
        steps.append(result)
        if result.get("escalate") or result.get("status") == "blocked":
            emit_progress(
                task_id,
                "phase_blocked",
                phase=phase,
                node_id=result.get("node_id"),
                status="execute_failed",
                message=result.get("message"),
            )
            return {
                "status": "failed",
                "stage": "execute",
                "task_id": task_id,
                "phase": phase,
                "steps": steps,
                "message": result.get("message", "escalate to premium agent — use planner_query_logs"),
            }
        if result.get("status") == "failed":
            continue
        if result.get("status") in ("success", "skipped"):
            continue
        return {
            "status": "failed",
            "stage": "execute",
            "task_id": task_id,
            "phase": phase,
            "steps": steps,
            "message": result.get("message", "unexpected execute state"),
        }

    dag = db.get_phase_dag(task_id, phase)
    executions = db.load_executions(task_id, phase)
    complete = phase_execution_complete(dag, executions) if dag else False
    phase_status = "completed" if complete else "incomplete"
    emit_progress(task_id, "phase_done", phase=phase, status=phase_status)
    return {
        "status": phase_status,
        "task_id": task_id,
        "phase": phase,
        "steps": steps,
        "execution_complete": complete,
    }


def eval_phase_internal(task_id: str, phase: int) -> dict[str, Any]:
    dag = db.get_phase_dag(task_id, phase)
    if not dag:
        return {"all_passed": False, "nodes": [], "error": "dag not found"}
    rev = dag.get("dag_revision")
    results = []
    for node in dag.get("nodes", []):
        nid = node["id"]
        latest = db.latest_node_eval(task_id, phase, nid, rev)
        if latest and latest.get("passed"):
            results.append({"node_id": nid, "passed": True, "cached": True})
            continue
        results.append(internal_eval_node(task_id, phase, nid))
    return {"all_passed": all(r.get("passed") for r in results), "nodes": results}
