"""Shared helpers and artifact validation for planner-exec."""

from __future__ import annotations

import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .pe_validate import validate_dag_nodes

VALID_SAVE_TYPES = {
    "goal-confirmed",
    "phases",
    "dag",
    "execution",
    "status",
}

DEFAULT_MAX_NODE_EVAL_ITERATIONS = 3
DEFAULT_MAX_NODE_EXECUTE_RETRIES = 2

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:max_len] or "task").rstrip("-")


def make_task_id(goal: str) -> str:
    date_part = datetime.now().strftime("%y%m%d")
    suffix = slugify(goal, 16) or secrets.token_hex(2)
    return f"{date_part}-{suffix}"


def require_task_id(task_id: str | None) -> str:
    if not task_id:
        raise SystemExit("ERROR: --task-id is required (no global current pointer)")
    return task_id


def ensure_task(task_id: str) -> str:
    if not db.task_exists(task_id):
        raise SystemExit(f"ERROR: task not found: {task_id}")
    return task_id


def read_stdin_json() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("ERROR: expected JSON on stdin")
    return json.loads(raw)


def read_data_arg(data_file: str | None, data_inline: str | None) -> Any:
    if data_file:
        return load_json(Path(data_file))
    if data_inline:
        return json.loads(data_inline)
    return read_stdin_json()


def validate_goal_confirmed(data: dict[str, Any]) -> None:
    required = ["goal", "success_criteria", "resources", "constraints", "assumptions"]
    missing = [k for k in required if k not in data]
    if missing:
        raise SystemExit(f"ERROR: goal-confirmed missing fields: {', '.join(missing)}")
    if not data.get("goal", "").strip():
        raise SystemExit("ERROR: goal must be non-empty")
    if not isinstance(data.get("success_criteria"), list) or not data["success_criteria"]:
        raise SystemExit("ERROR: success_criteria must be a non-empty list")
    if not isinstance(data.get("resources"), list):
        raise SystemExit("ERROR: resources must be a list")
    open_q = data.get("open_questions")
    if open_q is None:
        data["open_questions"] = []
    if data["open_questions"]:
        raise SystemExit("ERROR: open_questions must be empty before saving goal-confirmed")


def validate_phases(data: dict[str, Any]) -> None:
    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        raise SystemExit("ERROR: phases must be a non-empty list")
    ids: set[str] = set()
    for i, phase in enumerate(phases, start=1):
        for key in ("id", "title", "objective", "inputs", "outputs", "done_definition"):
            if key not in phase:
                raise SystemExit(f"ERROR: phase {i} missing field: {key}")
        if phase["id"] in ids:
            raise SystemExit(f"ERROR: duplicate phase id: {phase['id']}")
        ids.add(phase["id"])


def validate_dag(data: dict[str, Any]) -> None:
    validate_dag_nodes(data)
