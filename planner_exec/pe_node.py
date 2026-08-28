"""DAG node schema helpers (v2 natural-language nodes)."""

from __future__ import annotations

from typing import Any


def node_acceptance(node: dict[str, Any]) -> str:
    return (node.get("acceptance") or "").strip()


def node_dependencies(node: dict[str, Any]) -> list[str]:
    deps: list[str] = []
    for key in ("depends_on", "reads_from"):
        for dep in node.get(key) or []:
            if dep and dep not in deps:
                deps.append(dep)
    return deps


def slim_node_for_prompt(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "description": node.get("description"),
        "acceptance": node_acceptance(node),
        "acceptance_checks": node.get("acceptance_checks") or [],
        "reads_from": node.get("reads_from") or [],
        "depends_on": node_dependencies(node),
    }
