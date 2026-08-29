"""Mechanical validation for planner-exec artifacts."""

from __future__ import annotations

from typing import Any

from .pe_node import node_acceptance, node_dependencies

_ACCEPTANCE_CHECK_TYPES = frozenset({"file_exists", "shell", "file_contains"})


def _issue(node_id: str, severity: str, typ: str, message: str) -> dict[str, str]:
    return {"node_id": node_id, "severity": severity, "type": typ, "message": message}


def validate_dag_nodes(data: dict[str, Any]) -> None:
    """Validate DAG document (v2 natural-language nodes). Raises SystemExit on error."""
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise SystemExit("ERROR: dag.nodes must be a non-empty list")

    node_ids: set[str] = set()
    for node in nodes:
        node_id = node.get("id", "")
        if not node_id:
            raise SystemExit("ERROR: dag node missing field: id")
        if node_id in node_ids:
            raise SystemExit(f"ERROR: duplicate node id: {node_id}")
        node_ids.add(node_id)

        if not (node.get("description") or "").strip():
            raise SystemExit(f"ERROR: node {node_id} missing or empty description")
        if not node_acceptance(node):
            raise SystemExit(f"ERROR: node {node_id} must have non-empty acceptance")

        for check in node.get("acceptance_checks") or []:
            typ = check.get("type", "")
            if typ not in _ACCEPTANCE_CHECK_TYPES:
                raise SystemExit(
                    f"ERROR: node {node_id} acceptance_checks has unknown type: {typ!r}"
                )

    for node in nodes:
        node_id = node["id"]
        for dep in node_dependencies(node):
            if dep not in node_ids:
                raise SystemExit(f"ERROR: node {node_id} references unknown node {dep}")
            if dep == node_id:
                raise SystemExit(f"ERROR: node {node_id} depends on itself")

        cond = node.get("condition")
        if cond is not None:
            for key in ("expr", "true_next", "false_next"):
                if key not in cond:
                    raise SystemExit(f"ERROR: node {node_id} condition missing {key}")
            for branch_key in ("true_next", "false_next"):
                target = cond.get(branch_key)
                if target and target not in node_ids:
                    raise SystemExit(
                        f"ERROR: node {node_id} condition {branch_key} points to unknown node {target}"
                    )


def validate_node_mechanical(
    node: dict[str, Any],
    all_nodes: dict[str, dict[str, Any]],
    phase: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node_id = node.get("id", "<unknown>")
    issues: list[dict[str, str]] = []

    if not node.get("id"):
        issues.append(_issue(node_id, "blocker", "missing_field", "missing field: id"))
    if not (node.get("description") or "").strip():
        issues.append(_issue(node_id, "blocker", "missing_field", "missing or empty description"))

    if not node_acceptance(node):
        issues.append(_issue(node_id, "blocker", "acceptance", "acceptance is empty"))

    for dep in node_dependencies(node):
        if dep not in all_nodes:
            issues.append(_issue(node_id, "blocker", "dependency", f"unknown dependency: {dep}"))
        elif dep == node_id:
            issues.append(_issue(node_id, "blocker", "dependency", "node depends on itself"))

    for check in node.get("acceptance_checks") or []:
        typ = check.get("type", "")
        if not typ:
            issues.append(_issue(node_id, "blocker", "acceptance", "acceptance_checks entry missing type"))
        elif typ not in _ACCEPTANCE_CHECK_TYPES:
            issues.append(_issue(node_id, "blocker", "acceptance", f"unknown acceptance_checks type: {typ}"))

    cond = node.get("condition")
    if cond is not None:
        for key in ("expr", "true_next", "false_next"):
            if key not in cond:
                issues.append(_issue(node_id, "blocker", "condition", f"condition missing {key}"))
        for branch_key in ("true_next", "false_next"):
            target = cond.get(branch_key)
            if target and target not in all_nodes:
                issues.append(
                    _issue(node_id, "blocker", "condition", f"condition {branch_key} points to unknown node {target}")
                )

    blockers = [i for i in issues if i["severity"] == "blocker"]
    return {
        "passed": len(blockers) == 0,
        "issues": issues,
        "schema": "v2",
    }


def build_node_eval_context(
    node: dict[str, Any],
    all_nodes: dict[str, dict[str, Any]],
    dag: dict[str, Any],
    phase: dict[str, Any] | None,
    goal: dict[str, Any] | None,
) -> dict[str, Any]:
    upstream = {}
    for dep_id in node_dependencies(node):
        if dep_id in all_nodes:
            upstream[dep_id] = {
                "id": dep_id,
                "title": all_nodes[dep_id].get("title"),
                "description": all_nodes[dep_id].get("description"),
                "acceptance": node_acceptance(all_nodes[dep_id]),
            }

    siblings = []
    for other in all_nodes.values():
        oid = other.get("id")
        if not oid or oid == node.get("id"):
            continue
        siblings.append(
            {
                "id": oid,
                "description": other.get("description"),
                "acceptance": node_acceptance(other),
                "acceptance_checks": other.get("acceptance_checks") or [],
            }
        )

    return {
        "evaluation_scope": "single_node",
        "phase": {"id": (phase or {}).get("id"), "title": (phase or {}).get("title")},
        "goal_success_criteria": (goal or {}).get("success_criteria", []),
        "dag_meta": {
            "phase_id": dag.get("phase_id"),
            "completion_node": dag.get("completion_node"),
        },
        "node": {
            "id": node.get("id"),
            "title": node.get("title"),
            "description": node.get("description"),
            "acceptance": node_acceptance(node),
            "reads_from": node.get("reads_from") or [],
            "depends_on": node_dependencies(node),
            "acceptance_checks": node.get("acceptance_checks") or [],
        },
        "upstream_nodes": upstream,
        "phase_siblings": siblings,
        "all_node_ids": list(all_nodes.keys()),
    }
