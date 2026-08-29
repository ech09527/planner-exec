"""Atomic task planning: init + goal/phases/dags in one write."""

from __future__ import annotations

import os
from typing import Any

from . import db
from .pe_dag import dag_revision
from .pe_dag_eval import evaluate_plan_dags, save_dag_eval
from .pe_util import make_task_id, utc_now, validate_dag, validate_goal_confirmed, validate_phases


class PlanError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_plan_shape(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise PlanError("plan must be an object")
    if not (plan.get("goal") or "").strip():
        raise PlanError("plan.goal is required")
    if "goal_confirmed" not in plan:
        raise PlanError("plan.goal_confirmed is required")
    if "phases" not in plan:
        raise PlanError("plan.phases is required")
    if "dags" not in plan:
        raise PlanError("plan.dags is required")
    if not isinstance(plan["dags"], list):
        raise PlanError("plan.dags must be an array")


def _risk_flags(plan: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for dag_entry in plan.get("dags") or []:
        phase = dag_entry.get("phase")
        for node in dag_entry.get("nodes") or []:
            nid = node.get("id", "?")
            if not node.get("acceptance_checks"):
                flags.append(f"phase {phase} node {nid} has no acceptance_checks")
    return flags[:10]


def build_plan_summary(
    task_id: str,
    plan: dict[str, Any],
    *,
    dag_eval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phases_list = (plan.get("phases") or {}).get("phases") or []
    dags = plan.get("dags") or []
    node_total = sum(len(d.get("nodes") or []) for d in dags)
    check_total = sum(
        len(n.get("acceptance_checks") or [])
        for d in dags
        for n in (d.get("nodes") or [])
    )
    phase_summaries: list[dict[str, Any]] = []
    for dag_entry in dags:
        phase = dag_entry.get("phase")
        nodes = dag_entry.get("nodes") or []
        dag_body = {"nodes": nodes}
        rev = dag_revision(dag_body)
        title = None
        if isinstance(phase, int) and 0 < phase <= len(phases_list):
            title = phases_list[phase - 1].get("title")
        phase_summaries.append(
            {
                "phase": phase,
                "title": title,
                "nodes": len(nodes),
                "dag_revision": rev,
            }
        )

    risks = _risk_flags(plan)
    dag_ok = True if dag_eval is None else bool(dag_eval.get("passed"))
    ready = (
        len(phases_list) > 0
        and len(dags) == len(phases_list)
        and node_total > 0
        and dag_ok
    )
    out: dict[str, Any] = {
        "task_id": task_id,
        "status": "planned" if dag_ok else "dag_eval_failed",
        "summary": {
            "goal": (plan.get("goal_confirmed") or {}).get("goal") or plan.get("goal"),
            "phase_count": len(phases_list),
            "node_count": node_total,
            "acceptance_check_count": check_total,
            "phases": phase_summaries,
            "risk_flags": risks,
        },
        "ready_for_run": ready,
        "next": "planner_run" if ready else "revise plan then planner_plan (or replan patches)",
    }
    if dag_eval is not None:
        out["dag_eval"] = {
            "passed": dag_eval.get("passed"),
            "blocker_count": dag_eval.get("blocker_count"),
            "issues": (dag_eval.get("issues") or [])[:8],
            "suggestions": (dag_eval.get("suggestions") or [])[:5],
            "fingerprint": dag_eval.get("fingerprint"),
        }
    return out


def apply_plan(
    plan: dict[str, Any],
    *,
    task_id: str | None = None,
    workspace: str | None = None,
    agent_id: str | None = None,
    force: bool = False,
    validate_only: bool = False,
    max_node_eval_iterations: int = 3,
    max_node_execute_retries: int = 2,
    source: str = "mcp",
    skip_dag_llm: bool | None = None,
) -> dict[str, Any]:
    validate_plan_shape(plan)
    validate_goal_confirmed(plan["goal_confirmed"])
    validate_phases(plan["phases"])

    phases_list = plan["phases"]["phases"]
    phase_count = len(phases_list)
    dags = plan["dags"]

    if len(dags) != phase_count:
        raise PlanError(f"plan.dags length ({len(dags)}) must match phases count ({phase_count})")

    seen_phases: set[int] = set()
    for dag_entry in dags:
        if not isinstance(dag_entry, dict):
            raise PlanError("each plan.dags entry must be an object")
        phase = dag_entry.get("phase")
        if not isinstance(phase, int):
            raise PlanError("each plan.dags entry requires integer phase")
        if phase in seen_phases:
            raise PlanError(f"duplicate dag for phase {phase}")
        if phase < 1 or phase > phase_count:
            raise PlanError(f"dag phase {phase} out of range 1..{phase_count}")
        seen_phases.add(phase)
        nodes = dag_entry.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise PlanError(f"phase {phase} dag requires non-empty nodes")
        validate_dag({"nodes": nodes})

    for p in range(1, phase_count + 1):
        if p not in seen_phases:
            raise PlanError(f"missing dag for phase {p}")

    if skip_dag_llm is None:
        skip_dag_llm = os.environ.get("PE_SKIP_DAG_LLM", "").lower() in ("1", "true", "yes")

    tid = task_id or make_task_id(plan["goal"])
    # Evaluate before create_task — no task_id (FK would fail on agent_traces).
    dag_eval = evaluate_plan_dags(
        goal=plan.get("goal_confirmed"),
        phases_doc=plan["phases"],
        dags=dags,
        task_id=None,
        skip_llm=bool(skip_dag_llm),
    )

    if validate_only:
        out = build_plan_summary(tid, plan, dag_eval=dag_eval)
        out["validate_only"] = True
        return out

    if db.task_exists(tid) and not force:
        raise PlanError(f"task already exists: {tid} (use force=true to overwrite)", status=409)

    if force and db.task_exists(tid):
        db.delete_task(tid)

    now = utc_now()
    context = plan.get("context") or {}
    status = "planned" if dag_eval.get("passed") else "dag_eval_failed"
    meta = {
        "task_id": tid,
        "created_at": now,
        "updated_at": now,
        "status": status,
        "max_node_eval_iterations": max_node_eval_iterations,
        "max_node_execute_retries": max_node_execute_retries,
        "agent_id": agent_id,
        "workspace": workspace,
    }
    raw_goal = {
        "goal": plan["goal"].strip(),
        "context": context,
        "captured_at": now,
        "source": source,
    }

    db.create_task(meta, raw_goal)
    db.save_artifact(tid, "goal-confirmed", plan["goal_confirmed"], now)
    db.save_artifact(tid, "phases", plan["phases"], now)

    last_rev: str | None = None
    for dag_entry in sorted(dags, key=lambda d: d["phase"]):
        phase = dag_entry["phase"]
        dag_body = {"nodes": dag_entry["nodes"]}
        rev = dag_revision(dag_body)
        payload = {**dag_body, "saved_at": now, "phase": phase, "dag_revision": rev}
        db.save_phase_dag(tid, phase, rev, payload, now)
        last_rev = rev

    dag_eval = {**dag_eval, "task_id": tid}
    save_dag_eval(tid, dag_eval)

    db.update_task_meta(
        tid,
        updated_at=now,
        status=status,
        goal=plan["goal_confirmed"].get("goal"),
        phase_count=phase_count,
        dag_revision=last_rev,
    )

    return build_plan_summary(tid, plan, dag_eval=dag_eval)
