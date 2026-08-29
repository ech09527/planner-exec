"""DAG incremental patch operations (C2)."""

from __future__ import annotations

from typing import Any

from . import db
from .pe_dag import dag_revision
from .pe_dag_eval import invalidate_dag_eval
from .pe_node import node_acceptance, node_dependencies
from .pe_util import utc_now, validate_dag


class PatchError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def task_allows_patch(task_id: str) -> tuple[bool, str]:
    if db.is_task_running(task_id):
        return False, "task is running; wait for run_task/run_phase to finish"

    meta = db.get_task_meta(task_id)
    status = (meta.get("status") or "").lower()
    allowed_markers = (
        "blocked",
        "failed",
        "incomplete",
        "eval_failed",
        "dag_eval_failed",
        "execute_failed",
        "escalat",
        "phase_blocked",
        "dag_patched",
    )
    if any(marker in status for marker in allowed_markers):
        return True, ""

    latest = db.get_latest_progress_event(task_id) or {}
    if latest.get("event") == "phase_blocked":
        return True, ""
    if db.get_latest_escalation(task_id):
        return True, ""

    return False, "task is not blocked or incomplete; patch only allowed after failure"


def _node_is_referenced(node_id: str, nodes: list[dict[str, Any]]) -> str | None:
    for node in nodes:
        if node_id in node_dependencies(node):
            return node["id"]
        cond = node.get("condition") or {}
        for key in ("true_next", "false_next"):
            if cond.get(key) == node_id:
                return node["id"]
    return None


def _validate_new_node(node: dict[str, Any]) -> str:
    node_id = node.get("id")
    if not node_id:
        raise PatchError("insert_after node missing id")
    if not (node.get("description") or "").strip():
        raise PatchError(f"node {node_id} missing or empty description")
    if not node_acceptance(node):
        raise PatchError(f"node {node_id} must have non-empty acceptance")
    return node_id


def _apply_patch(nodes: list[dict[str, Any]], patch: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    op = patch.get("op")
    nodes_by_id = {n["id"]: n for n in nodes}
    changed: list[str] = []

    if op == "replace":
        node_id = patch.get("node_id")
        if not node_id:
            raise PatchError("patch missing node_id")
        if node_id not in nodes_by_id:
            raise PatchError(f"node not found: {node_id}")
        replacement = patch.get("node")
        if not isinstance(replacement, dict):
            raise PatchError(f"patch for {node_id} missing node object")
        merged = {**nodes_by_id[node_id], **replacement, "id": node_id}
        nodes = [merged if n["id"] == node_id else n for n in nodes]
        changed.append(node_id)

    elif op == "delete":
        node_id = patch.get("node_id")
        if not node_id:
            raise PatchError("patch missing node_id")
        if node_id not in nodes_by_id:
            raise PatchError(f"node not found: {node_id}")
        ref = _node_is_referenced(node_id, nodes)
        if ref:
            raise PatchError(f"cannot delete {node_id}: referenced by {ref}")
        nodes = [n for n in nodes if n["id"] != node_id]
        if not nodes:
            raise PatchError("cannot delete last node in dag")
        changed.append(node_id)

    elif op == "insert_after":
        after = patch.get("after")
        node = patch.get("node")
        if not after:
            raise PatchError("insert_after missing after")
        if after not in nodes_by_id:
            raise PatchError(f"after node not found: {after}")
        if not isinstance(node, dict):
            raise PatchError("insert_after missing node object")
        new_id = _validate_new_node(node)
        if new_id in nodes_by_id:
            raise PatchError(f"duplicate node id: {new_id}")
        new_node = {**node, "id": new_id}
        updated: list[dict[str, Any]] = []
        inserted = False
        for current in nodes:
            updated.append(current)
            if current["id"] == after:
                updated.append(new_node)
                inserted = True
        if not inserted:
            raise PatchError(f"failed to insert after {after}")
        nodes = updated
        changed.append(new_id)

    else:
        raise PatchError(f"unsupported patch op: {op!r}")

    return nodes, changed


def apply_node_patches(task_id: str, phase: int, patches: list[dict[str, Any]]) -> dict[str, Any]:
    allowed, reason = task_allows_patch(task_id)
    if not allowed:
        raise PatchError(reason, status=409)

    dag = db.get_phase_dag(task_id, phase)
    if not dag:
        raise PatchError(f"dag not found for phase {phase}")

    nodes = list(dag.get("nodes") or [])
    changed: list[str] = []

    for patch in patches:
        nodes, patch_changed = _apply_patch(nodes, patch)
        changed.extend(patch_changed)

    if not changed:
        raise PatchError("no patches applied")

    base = {k: v for k, v in dag.items() if k not in ("dag_revision", "saved_at")}
    base["nodes"] = nodes
    base["phase"] = phase
    validate_dag(base)

    rev = dag_revision(base)
    now = utc_now()
    payload = {**base, "saved_at": now, "phase": phase, "dag_revision": rev}
    db.save_phase_dag(task_id, phase, rev, payload, now)
    invalidate_dag_eval(task_id)
    db.update_task_meta(
        task_id,
        updated_at=now,
        status=f"dag_patched_phase_{phase:02d}",
        dag_revision=rev,
    )

    return {
        "ok": True,
        "task_id": task_id,
        "phase": phase,
        "dag_revision": rev,
        "changed_nodes": changed,
    }
