"""Planner-Exec task persistence and per-node DAG evaluation CLI.

All task data is stored under planner_data_root() (default ~/.planer-exec/planner.db).
No global current pointer — every command requires an explicit --task-id.
"""

from __future__ import annotations

from .pe_cli import (
    build_parser,
    cmd_eval_node,
    cmd_eval_phase,
    cmd_eval_status,
    cmd_execute_node,
    cmd_init,
    cmd_list,
    cmd_migrate,
    cmd_next_node,
    cmd_progress,
    cmd_query_logs,
    cmd_run_phase,
    cmd_run_task,
    cmd_save,
    cmd_show,
    cmd_status,
    main,
)
from .pe_orchestrate import (
    compute_progress,
    eval_phase_internal,
    internal_eval_node,
    internal_execute_node,
    internal_run_phase,
)
from .pe_paths import legacy_tasks_dir, planner_data_root
from .pe_util import (
    DEFAULT_MAX_NODE_EVAL_ITERATIONS,
    DEFAULT_MAX_NODE_EXECUTE_RETRIES,
    VALID_SAVE_TYPES,
    ensure_task,
    make_task_id,
    require_task_id,
    utc_now,
    validate_dag,
)

__all__ = [
    "DEFAULT_MAX_NODE_EVAL_ITERATIONS",
    "DEFAULT_MAX_NODE_EXECUTE_RETRIES",
    "VALID_SAVE_TYPES",
    "build_parser",
    "cmd_eval_node",
    "cmd_eval_phase",
    "cmd_eval_status",
    "cmd_execute_node",
    "cmd_init",
    "cmd_list",
    "cmd_migrate",
    "cmd_next_node",
    "cmd_progress",
    "cmd_query_logs",
    "cmd_run_phase",
    "cmd_run_task",
    "cmd_save",
    "cmd_show",
    "cmd_status",
    "compute_progress",
    "ensure_task",
    "eval_phase_internal",
    "internal_eval_node",
    "internal_execute_node",
    "internal_run_phase",
    "legacy_tasks_dir",
    "main",
    "make_task_id",
    "planner_data_root",
    "require_task_id",
    "utc_now",
    "validate_dag",
]
