"""DAG utilities: revision hash, input resolution, execution planning."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .pe_node import node_dependencies


def dag_revision(dag: dict[str, Any]) -> str:
    payload = json.dumps(dag, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_execution_outputs(executions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for record in executions:
        if record.get("status") == "success" and record.get("node_id"):
            outputs[record["node_id"]] = record.get("outputs") or {}
    return outputs


def resolve_node_inputs(
    node: dict[str, Any],
    executions: list[dict[str, Any]],
    goal: dict[str, Any] | None = None,
    raw_goal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    del goal, raw_goal  # v2 resolves upstream node outputs only
    node_outputs = build_execution_outputs(executions)
    resolved: dict[str, Any] = {}
    errors: list[str] = []
    for dep in node_dependencies(node):
        if dep not in node_outputs:
            errors.append(f"waiting on upstream node {dep}")
        else:
            resolved[dep] = node_outputs[dep]
    return resolved, errors


def deps_satisfied(node: dict[str, Any], executions: list[dict[str, Any]]) -> bool:
    done = {
        e["node_id"]
        for e in executions
        if e.get("status") in ("success", "skipped") and e.get("node_id")
    }
    return all(dep in done for dep in node_dependencies(node))


def node_failed_count(executions: list[dict[str, Any]], node_id: str) -> int:
    return sum(1 for e in executions if e.get("node_id") == node_id and e.get("status") == "failed")


def node_execution_record(executions: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    records = [e for e in executions if e.get("node_id") == node_id]
    return records[-1] if records else None


def should_skip_node(node: dict[str, Any], executions: list[dict[str, Any]]) -> tuple[bool, str | None]:
    cond = node.get("condition")
    if not cond:
        return False, None
    expr = cond.get("expr", "")
    match = re.match(r"^([^.]+)\.outputs\.([^.\s]+)\s*==\s*(true|false)$", expr.strip(), re.I)
    if not match:
        return False, None
    src_node, field, expected = match.group(1), match.group(2), match.group(3).lower() == "true"
    outputs = build_execution_outputs(executions).get(src_node, {})
    actual = outputs.get(field)
    if actual is None:
        return False, None
    if bool(actual) != expected:
        return True, f"condition not met: {expr}"
    return False, None


def topological_order(nodes: list[dict[str, Any]]) -> list[str]:
    by_id = {n["id"]: n for n in nodes}
    visited: set[str] = set()
    order: list[str] = []

    def visit(nid: str) -> None:
        if nid in visited:
            return
        visited.add(nid)
        for dep in node_dependencies(by_id.get(nid, {})):
            visit(dep)
        order.append(nid)

    for node in nodes:
        visit(node["id"])
    return order


def phase_execution_complete(dag: dict[str, Any], executions: list[dict[str, Any]]) -> bool:
    for node in dag.get("nodes", []):
        nid = node["id"]
        record = node_execution_record(executions, nid)
        if record and record.get("status") in ("success", "skipped"):
            continue
        if record and record.get("status") == "failed":
            return False
        return False
    return True


def next_executable_node(
    dag: dict[str, Any],
    executions: list[dict[str, Any]],
    max_retries: int = 2,
) -> dict[str, Any] | None:
    nodes = dag.get("nodes", [])
    order = topological_order(nodes)
    by_id = {n["id"]: n for n in nodes}

    for nid in order:
        record = node_execution_record(executions, nid)
        if record and record.get("status") in ("success", "skipped"):
            continue
        if record and record.get("status") == "failed":
            if node_failed_count(executions, nid) >= max_retries:
                return {
                    "node": by_id[nid],
                    "action": "blocked",
                    "reason": f"max execute retries ({max_retries}) exceeded for {nid}",
                }

        node = by_id[nid]
        if not deps_satisfied(node, executions):
            continue

        skip, reason = should_skip_node(node, executions)
        if skip:
            return {"node": node, "action": "skip", "reason": reason}
        return {"node": node, "action": "execute", "reason": None}

    return None
