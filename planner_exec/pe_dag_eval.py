"""Whole-DAG plan evaluation (mechanical + optional LLM).

Roles:
  dag_eval   — graph-level plan quality (this module)
  node_eval  — per-node plan quality (pe_agent validate)
  node_execute / node_verify — execute + mechanical acceptance
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import db
from .pe_dag import dag_revision
from .pe_llm import evaluate_dag_with_llm
from .pe_node import slim_node_for_prompt
from .pe_util import utc_now

ARTIFACT_KIND = "dag-eval"

_INTEGRATION_HINT = re.compile(
    r"(unittest|pytest|集成|integration|跑.?测|shell\s*验|cli\s*验|grep\s*-q)",
    re.IGNORECASE,
)


def plan_fingerprint(dags: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for entry in sorted(dags, key=lambda d: int(d.get("phase") or 0)):
        phase = entry.get("phase")
        nodes = entry.get("nodes") or []
        rev = dag_revision({"nodes": nodes})
        parts.append(f"{phase}:{rev}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint_from_task(task_id: str) -> str | None:
    phases_doc = db.get_artifact(task_id, "phases") or {}
    phases = phases_doc.get("phases") or []
    if not phases:
        return None
    dags: list[dict[str, Any]] = []
    for idx in range(1, len(phases) + 1):
        dag = db.get_phase_dag(task_id, idx)
        if not dag:
            return None
        dags.append({"phase": idx, "nodes": dag.get("nodes") or []})
    return plan_fingerprint(dags)


def _phase_has_strong_check(nodes: list[dict[str, Any]], from_index: int = 0) -> bool:
    for node in nodes[from_index:]:
        for check in node.get("acceptance_checks") or []:
            typ = check.get("type")
            if typ in ("shell", "file_contains"):
                return True
    return False


def mechanical_dag_eval(
    *,
    goal: dict[str, Any] | None,
    phases: list[dict[str, Any]],
    dags: list[dict[str, Any]],
) -> dict[str, Any]:
    """Graph-level mechanical checks (no LLM)."""
    issues: list[dict[str, Any]] = []
    by_phase = {int(d["phase"]): d.get("nodes") or [] for d in dags if d.get("phase") is not None}

    for phase, nodes in sorted(by_phase.items()):
        if not nodes:
            issues.append(
                {
                    "severity": "blocker",
                    "type": "empty_dag",
                    "phase": phase,
                    "node_id": None,
                    "message": f"phase {phase} has no nodes",
                }
            )
            continue

        for i, node in enumerate(nodes):
            nid = node.get("id") or f"phase{phase}#{i}"
            checks = node.get("acceptance_checks") or []
            if not checks:
                issues.append(
                    {
                        "severity": "blocker",
                        "type": "missing_checks",
                        "phase": phase,
                        "node_id": nid,
                        "message": f"node {nid} has no acceptance_checks",
                    }
                )
                continue

            desc = node.get("description") or ""
            only_exists = checks and all(c.get("type") == "file_exists" for c in checks)
            if only_exists and _INTEGRATION_HINT.search(desc):
                later_same = _phase_has_strong_check(nodes, i + 1)
                later_other = any(
                    _phase_has_strong_check(by_phase[p], 0) for p in by_phase if p > phase
                )
                if not later_same and not later_other:
                    issues.append(
                        {
                            "severity": "blocker",
                            "type": "ungrounded_acceptance",
                            "phase": phase,
                            "node_id": nid,
                            "message": (
                                f"node {nid} describes integration/tests but only has file_exists, "
                                "and no later node has shell/file_contains to cover it"
                            ),
                        }
                    )

        criteria = " ".join((goal or {}).get("success_criteria") or [])
        if phase == max(by_phase) and _INTEGRATION_HINT.search(criteria):
            if not _phase_has_strong_check(nodes, 0):
                issues.append(
                    {
                        "severity": "warning",
                        "type": "weak_final_checks",
                        "phase": phase,
                        "node_id": nodes[-1].get("id"),
                        "message": (
                            "goal success_criteria mention tests/CLI but no shell/file_contains "
                            f"in phase {phase}"
                        ),
                    }
                )

    blockers = [i for i in issues if i.get("severity") == "blocker"]
    return {
        "passed": len(blockers) == 0,
        "issues": issues,
        "blocker_count": len(blockers),
        "source": "mechanical",
    }


def build_dag_eval_context(
    *,
    goal: dict[str, Any] | None,
    phases: list[dict[str, Any]],
    dags: list[dict[str, Any]],
    task_id: str | None = None,
) -> dict[str, Any]:
    slim_phases = []
    for entry in sorted(dags, key=lambda d: int(d.get("phase") or 0)):
        phase = int(entry["phase"])
        phase_def = phases[phase - 1] if 0 < phase <= len(phases) else {}
        nodes = [slim_node_for_prompt(n) for n in (entry.get("nodes") or [])]
        slim_phases.append(
            {
                "phase": phase,
                "title": phase_def.get("title"),
                "objective": phase_def.get("objective"),
                "done_definition": phase_def.get("done_definition"),
                "nodes": nodes,
            }
        )
    return {
        "task_id": task_id,
        "goal": (goal or {}).get("goal"),
        "success_criteria": (goal or {}).get("success_criteria") or [],
        "phases": slim_phases,
        "rules": [
            "中间节点只用 file_exists 是正常的",
            "集成 shell/unittest 应落在该 phase 或后续 phase 的靠后节点",
            "仅当整图无法机械验证 success_criteria / 节点承诺时才 blocker",
            "不要因为 workspace 尚无产物而失败",
        ],
    }


def evaluate_plan_dags(
    *,
    goal: dict[str, Any] | None,
    phases_doc: dict[str, Any],
    dags: list[dict[str, Any]],
    task_id: str | None = None,
    skip_llm: bool = False,
) -> dict[str, Any]:
    phases = (phases_doc or {}).get("phases") or []
    fingerprint = plan_fingerprint(dags)
    mechanical = mechanical_dag_eval(goal=goal, phases=phases, dags=dags)

    llm_result: dict[str, Any] | None = None
    if not skip_llm and mechanical.get("passed"):
        context = build_dag_eval_context(
            goal=goal, phases=phases, dags=dags, task_id=task_id
        )
        llm_result = evaluate_dag_with_llm(context)
    elif skip_llm or not mechanical.get("passed"):
        llm_result = {
            "passed": True if skip_llm else None,
            "skipped": True,
            "reason": "skip_llm" if skip_llm else "mechanical_failed",
            "issues": [],
            "suggestions": [],
        }

    combined_issues = list(mechanical.get("issues") or [])
    if llm_result and not llm_result.get("skipped") and llm_result.get("passed") is False:
        for issue in llm_result.get("issues") or []:
            combined_issues.append(
                {
                    "severity": issue.get("severity", "blocker"),
                    "type": issue.get("type", "dag_quality"),
                    "phase": issue.get("phase"),
                    "node_id": issue.get("node_id"),
                    "message": issue.get("message", ""),
                    "source": "llm",
                }
            )

    if not mechanical.get("passed"):
        passed = False
    elif llm_result is None:
        passed = False
        combined_issues.append(
            {
                "severity": "blocker",
                "type": "llm",
                "message": "DAG LLM eval did not run",
                "source": "system",
            }
        )
    elif llm_result.get("skipped"):
        # No API key / explicit skip: mechanical-only gate (keeps unit tests offline).
        passed = mechanical.get("passed") is True
    else:
        passed = llm_result.get("passed") is True

    return {
        "passed": passed,
        "fingerprint": fingerprint,
        "evaluated_at": utc_now(),
        "mechanical": mechanical,
        "llm": llm_result,
        "issues": combined_issues,
        "blocker_count": sum(1 for i in combined_issues if i.get("severity") == "blocker"),
        "suggestions": list((llm_result or {}).get("suggestions") or []),
        "next_step": (
            "planner_run"
            if passed
            else "revise plan (planner_plan again or planner_replan patches) then re-run"
        ),
    }


def save_dag_eval(task_id: str, result: dict[str, Any]) -> None:
    db.save_artifact(task_id, ARTIFACT_KIND, result, result.get("evaluated_at") or utc_now())


def load_dag_eval(task_id: str) -> dict[str, Any] | None:
    data = db.get_artifact(task_id, ARTIFACT_KIND)
    return data if isinstance(data, dict) else None


def invalidate_dag_eval(task_id: str) -> None:
    """Mark cached DAG eval stale after patches."""
    db.save_artifact(
        task_id,
        ARTIFACT_KIND,
        {
            "passed": False,
            "stale": True,
            "fingerprint": None,
            "evaluated_at": utc_now(),
            "issues": [
                {
                    "severity": "blocker",
                    "type": "stale",
                    "message": "DAG changed; dag_eval cache invalidated",
                }
            ],
            "blocker_count": 1,
        },
        utc_now(),
    )


def evaluate_task_dags(task_id: str, *, skip_llm: bool = False) -> dict[str, Any]:
    goal = db.get_artifact(task_id, "goal-confirmed")
    phases_doc = db.get_artifact(task_id, "phases") or {"phases": []}
    phases = phases_doc.get("phases") or []
    dags: list[dict[str, Any]] = []
    for idx in range(1, len(phases) + 1):
        dag = db.get_phase_dag(task_id, idx)
        if not dag:
            return {
                "passed": False,
                "error": f"missing dag for phase {idx}",
                "issues": [
                    {
                        "severity": "blocker",
                        "type": "missing_dag",
                        "phase": idx,
                        "message": f"missing dag for phase {idx}",
                    }
                ],
                "blocker_count": 1,
            }
        dags.append({"phase": idx, "nodes": dag.get("nodes") or []})

    result = evaluate_plan_dags(
        goal=goal,
        phases_doc=phases_doc,
        dags=dags,
        task_id=task_id,
        skip_llm=skip_llm,
    )
    save_dag_eval(task_id, result)
    return result


def ensure_dag_eval_passed(task_id: str) -> dict[str, Any]:
    """Run gate: reuse cache if fingerprint matches and passed."""
    fp = fingerprint_from_task(task_id)
    cached = load_dag_eval(task_id)
    if (
        cached
        and not cached.get("stale")
        and cached.get("passed")
        and fp
        and cached.get("fingerprint") == fp
    ):
        return {**cached, "cached": True}

    return {**evaluate_task_dags(task_id), "cached": False}
