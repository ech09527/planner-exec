"""SQLite storage for planner-exec (WAL mode, multi-process safe reads)."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .pe_paths import planner_data_root

HOME = Path.home()
ROOT = planner_data_root()
DB_PATH = ROOT / "planner.db"
SCHEMA_VERSION = 1


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _json_loads(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


@contextmanager
def connect(retries: int = 5, delay: float = 0.2) -> Iterator[sqlite3.Connection]:
    ROOT.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
            finally:
                conn.close()
            return
        except sqlite3.OperationalError as exc:
            last_err = exc
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))
    if last_err:
        raise last_err


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            goal TEXT,
            workspace TEXT,
            agent_id TEXT,
            status TEXT NOT NULL DEFAULT 'initialized',
            max_node_eval_iterations INTEGER NOT NULL DEFAULT 3,
            max_node_execute_retries INTEGER NOT NULL DEFAULT 2,
            phase_count INTEGER,
            dag_revision TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            data TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (task_id, kind),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS phase_dags (
            task_id TEXT NOT NULL,
            phase INTEGER NOT NULL,
            dag_revision TEXT NOT NULL,
            data TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (task_id, phase),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS node_evals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            phase INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            dag_revision TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            data TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            UNIQUE(task_id, phase, node_id, dag_revision, iteration),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_node_evals_lookup
            ON node_evals(task_id, phase, node_id, dag_revision, iteration DESC);

        CREATE TABLE IF NOT EXISTS node_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            phase INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_executions_lookup
            ON node_executions(task_id, phase, id);

        CREATE TABLE IF NOT EXISTS status_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            data TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            phase INTEGER NOT NULL,
            data TEXT NOT NULL,
            escalated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_escalations_lookup
            ON escalations(task_id, phase, id DESC);

        CREATE TABLE IF NOT EXISTS agent_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            phase INTEGER,
            node_id TEXT,
            agent_role TEXT NOT NULL,
            step INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_agent_traces_lookup
            ON agent_traces(task_id, phase, node_id, id);

        CREATE TABLE IF NOT EXISTS token_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            source TEXT NOT NULL,
            tool_or_role TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            response_chars INTEGER NOT NULL DEFAULT 0,
            meta TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_token_ledger_task
            ON token_ledger(task_id, id);

        CREATE TABLE IF NOT EXISTS task_sessions (
            task_id TEXT PRIMARY KEY,
            last_since TEXT,
            last_status_line TEXT,
            poll_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );
        """
    )


def task_exists(task_id: str) -> bool:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return row is not None


def create_task(meta: dict[str, Any], raw_goal: dict[str, Any]) -> None:
    init_db()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, goal, workspace, agent_id, status,
                    max_node_eval_iterations, max_node_execute_retries,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta["task_id"],
                    raw_goal.get("goal"),
                    meta.get("workspace"),
                    meta.get("agent_id"),
                    meta.get("status", "initialized"),
                    meta.get("max_node_eval_iterations", 3),
                    meta.get("max_node_execute_retries", 2),
                    meta["created_at"],
                    meta["updated_at"],
                ),
            )
            conn.execute(
                "INSERT INTO artifacts(task_id, kind, data, saved_at) VALUES (?, 'goal-raw', ?, ?)",
                (meta["task_id"], _json_dumps(raw_goal), meta["created_at"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_task_meta(task_id: str) -> dict[str, Any]:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return dict(row)


def update_task_meta(task_id: str, **fields: Any) -> dict[str, Any]:
    init_db()
    allowed = {
        "goal",
        "workspace",
        "agent_id",
        "status",
        "max_node_eval_iterations",
        "max_node_execute_retries",
        "phase_count",
        "dag_revision",
        "updated_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_task_meta(task_id)
    cols = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [task_id]
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE task_id=?", values)
    return get_task_meta(task_id)


def save_artifact(task_id: str, kind: str, data: Any, saved_at: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts(task_id, kind, data, saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id, kind) DO UPDATE SET data=excluded.data, saved_at=excluded.saved_at
            """,
            (task_id, kind, _json_dumps(data), saved_at),
        )


def get_artifact(task_id: str, kind: str) -> Any | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT data FROM artifacts WHERE task_id=? AND kind=?",
            (task_id, kind),
        ).fetchone()
        return _json_loads(row["data"]) if row else None


def save_phase_dag(task_id: str, phase: int, dag_revision: str, data: dict[str, Any], saved_at: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO phase_dags(task_id, phase, dag_revision, data, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, phase) DO UPDATE SET
                dag_revision=excluded.dag_revision,
                data=excluded.data,
                saved_at=excluded.saved_at
            """,
            (task_id, phase, dag_revision, _json_dumps(data), saved_at),
        )


def get_phase_dag(task_id: str, phase: int) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT data FROM phase_dags WHERE task_id=? AND phase=?",
            (task_id, phase),
        ).fetchone()
        return _json_loads(row["data"]) if row else None


def save_node_eval(result: dict[str, Any]) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO node_evals(
                task_id, phase, node_id, dag_revision, iteration, passed, data, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, phase, node_id, dag_revision, iteration) DO UPDATE SET
                passed=excluded.passed,
                data=excluded.data,
                evaluated_at=excluded.evaluated_at
            """,
            (
                result["task_id"],
                result["phase"],
                result["node_id"],
                result["dag_revision"],
                result["iteration"],
                1 if result.get("passed") else 0,
                _json_dumps(result),
                result.get("evaluated_at"),
            ),
        )


def latest_node_eval(
    task_id: str,
    phase: int,
    node_id: str,
    dag_revision: str | None = None,
) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        if dag_revision:
            row = conn.execute(
                """
                SELECT data FROM node_evals
                WHERE task_id=? AND phase=? AND node_id=? AND dag_revision=?
                ORDER BY iteration DESC LIMIT 1
                """,
                (task_id, phase, node_id, dag_revision),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT data FROM node_evals
                WHERE task_id=? AND phase=? AND node_id=?
                ORDER BY iteration DESC LIMIT 1
                """,
                (task_id, phase, node_id),
            ).fetchone()
        return _json_loads(row["data"]) if row else None


def append_execution(task_id: str, phase: int, entry: dict[str, Any]) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO node_executions(task_id, phase, node_id, status, data, saved_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                phase,
                entry.get("node_id", ""),
                entry.get("status", ""),
                _json_dumps(entry),
                entry.get("saved_at"),
            ),
        )


def load_executions(task_id: str, phase: int) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT data FROM node_executions WHERE task_id=? AND phase=? ORDER BY id",
            (task_id, phase),
        ).fetchall()
        return [_json_loads(r["data"]) for r in rows]


def append_status_snapshot(task_id: str, data: dict[str, Any], saved_at: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO status_snapshots(task_id, data, saved_at) VALUES (?, ?, ?)",
            (task_id, _json_dumps(data), saved_at),
        )


def get_latest_progress_event(task_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT data, saved_at FROM status_snapshots
            WHERE task_id=? AND json_extract(data, '$.kind')='progress'
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if not row:
            return None
        data = _json_loads(row["data"]) or {}
        data["saved_at"] = row["saved_at"]
        return data


def write_escalation(task_id: str, phase: int, payload: dict[str, Any], escalated_at: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO escalations(task_id, phase, data, escalated_at) VALUES (?, ?, ?, ?)",
            (task_id, phase, _json_dumps(payload), escalated_at),
        )


def get_latest_escalation(task_id: str, phase: int | None = None) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        sql = "SELECT phase, data, escalated_at FROM escalations WHERE task_id=?"
        params: list[Any] = [task_id]
        if phase is not None:
            sql += " AND phase=?"
            params.append(phase)
        sql += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        data = _json_loads(row["data"]) or {}
        return {
            "phase": row["phase"],
            "escalated_at": row["escalated_at"],
            **data,
        }


_RUNNING_PROGRESS_EVENTS = frozenset({"task_start", "phase_start", "node_start"})


def is_task_running(task_id: str) -> bool:
    latest = get_latest_progress_event(task_id)
    if not latest:
        return False
    return latest.get("event") in _RUNNING_PROGRESS_EVENTS and latest.get("status") == "running"


def record_token_ledger(
    task_id: str,
    *,
    source: str,
    tool_or_role: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    response_chars: int = 0,
    meta: dict[str, Any] | None = None,
    created_at: str,
) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO token_ledger(
                task_id, source, tool_or_role, input_tokens, output_tokens,
                response_chars, meta, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                source,
                tool_or_role,
                input_tokens,
                output_tokens,
                response_chars,
                _json_dumps(meta or {}),
                created_at,
            ),
        )


def upsert_task_session(
    task_id: str,
    *,
    last_since: str | None = None,
    last_status_line: str | None = None,
    poll_count: int | None = None,
    updated_at: str,
) -> dict[str, Any]:
    init_db()
    existing = get_task_session(task_id)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO task_sessions(task_id, last_since, last_status_line, poll_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                last_since=COALESCE(excluded.last_since, task_sessions.last_since),
                last_status_line=COALESCE(excluded.last_status_line, task_sessions.last_status_line),
                poll_count=COALESCE(excluded.poll_count, task_sessions.poll_count),
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                last_since if last_since is not None else (existing or {}).get("last_since"),
                last_status_line if last_status_line is not None else (existing or {}).get("last_status_line"),
                poll_count if poll_count is not None else (existing or {}).get("poll_count", 0),
                updated_at,
            ),
        )
    return get_task_session(task_id) or {}


def get_task_session(task_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM task_sessions WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks() -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def delete_task(task_id: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))


def migrate_json_task(task_dir: Path, task_id: str | None = None) -> str:
    """Import a legacy JSON task directory into SQLite."""
    init_db()
    tid = task_id or task_dir.name
    task_json = task_dir / "task.json"
    if not task_json.exists():
        raise FileNotFoundError(f"no task.json in {task_dir}")

    meta = json.loads(task_json.read_text(encoding="utf-8"))
    meta["task_id"] = tid

    if task_exists(tid):
        return tid

    raw_goal = json.loads((task_dir / "00-goal-raw.json").read_text(encoding="utf-8"))
    create_task(meta, raw_goal)

    for kind, filename in (
        ("goal-confirmed", "01-goal-confirmed.json"),
        ("phases", "02-phases.json"),
    ):
        path = task_dir / filename
        if path.exists():
            save_artifact(tid, kind, json.loads(path.read_text(encoding="utf-8")), meta.get("updated_at", ""))

    phases_root = task_dir / "phases"
    if phases_root.exists():
        for phase_dir in sorted(phases_root.glob("phase-*")):
            phase_num = int(phase_dir.name.split("-")[1])
            dag_path = phase_dir / "dag.json"
            if dag_path.exists():
                dag = json.loads(dag_path.read_text(encoding="utf-8"))
                save_phase_dag(
                    tid,
                    phase_num,
                    dag.get("dag_revision", ""),
                    dag,
                    dag.get("saved_at", meta.get("updated_at", "")),
                )
            evals_dir = phase_dir / "node-evals"
            if evals_dir.exists():
                dag_rev = dag.get("dag_revision", "") if dag_path.exists() else ""
                for eval_file in sorted(evals_dir.glob("*-iter-*.json")):
                    result = json.loads(eval_file.read_text(encoding="utf-8"))
                    result.setdefault("dag_revision", dag_rev)
                    save_node_eval(result)
            exec_path = phase_dir / "execution.jsonl"
            if exec_path.exists():
                for line in exec_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        append_execution(tid, phase_num, json.loads(line))
            esc_path = phase_dir / "escalation.json"
            if esc_path.exists():
                esc = json.loads(esc_path.read_text(encoding="utf-8"))
                write_escalation(tid, phase_num, esc, esc.get("escalated_at", ""))

    snap_path = task_dir / "status-snapshots.jsonl"
    if snap_path.exists():
        for line in snap_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                snap = json.loads(line)
                append_status_snapshot(tid, snap, snap.get("saved_at", ""))

    return tid


def migrate_all_json_tasks(tasks_dir: Path | None = None) -> list[str]:
    root = tasks_dir or (ROOT / "tasks")
    if not root.exists():
        return []
    imported: list[str] = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "task.json").exists():
            imported.append(migrate_json_task(path))
    return imported


def append_agent_trace(
    task_id: str,
    agent_role: str,
    event_type: str,
    data: dict[str, Any],
    created_at: str,
    phase: int | None = None,
    node_id: str | None = None,
    step: int = 0,
) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_traces(
                task_id, phase, node_id, agent_role, step, event_type, data, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                phase,
                node_id,
                agent_role,
                step,
                event_type,
                _json_dumps(data),
                created_at,
            ),
        )


def _summarize_eval(data: dict[str, Any]) -> str:
    passed = data.get("passed")
    issues = data.get("issues") or []
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    if passed:
        return f"eval passed (iteration {data.get('iteration')})"
    msgs = [i.get("message", "") for i in blockers[:3]]
    return f"eval failed: {'; '.join(msgs) or 'see issues'}"


def _summarize_execution(data: dict[str, Any]) -> str:
    status = data.get("status", "unknown")
    if status == "success":
        checks = data.get("acceptance_check") or []
        return f"execution success: {checks[0] if checks else 'ok'}"
    err = data.get("error") or ""
    return f"execution {status}: {err[:200]}"


def _summarize_escalation(data: dict[str, Any]) -> str:
    reason = data.get("reason", "unknown")
    node = data.get("node_id", "?")
    hint = data.get("hint", "")
    return f"escalation on {node}: {reason}" + (f" — {hint}" if hint else "")


def _build_replan_hints(entries: list[dict[str, Any]], failures: list[dict[str, Any]]) -> list[Any]:
    hints: list[Any] = []
    seen: set[str] = set()

    for entry in entries:
        if entry.get("type") != "agent_trace":
            continue
        meta = entry.get("trace_meta") or {}
        if meta.get("ok") is not False:
            continue
        key = f"{entry.get('node_id')}:{meta.get('tool')}:{str(meta.get('error', ''))[:80]}"
        if key in seen:
            continue
        seen.add(key)
        hints.append(
            {
                "kind": "tool_failure",
                "node_id": entry.get("node_id"),
                "phase": entry.get("phase"),
                "tool": meta.get("tool"),
                "error": meta.get("error"),
                "summary": entry.get("summary", ""),
            }
        )

    for entry in entries:
        if entry.get("type") != "execution" or entry.get("status") != "failed":
            continue
        key = f"exec:{entry.get('node_id')}:{entry.get('summary', '')[:80]}"
        if key in seen:
            continue
        seen.add(key)
        hints.append(
            {
                "kind": "execution_failure",
                "node_id": entry.get("node_id"),
                "phase": entry.get("phase"),
                "summary": entry.get("summary", ""),
            }
        )

    for entry in entries:
        if entry.get("type") != "escalation":
            continue
        key = f"esc:{entry.get('node_id')}:{entry.get('summary', '')[:80]}"
        if key in seen:
            continue
        seen.add(key)
        hints.append(
            {
                "kind": "escalation",
                "node_id": entry.get("node_id"),
                "phase": entry.get("phase"),
                "summary": entry.get("summary", ""),
            }
        )

    for entry in entries:
        meta = entry.get("failure_meta") or {}
        if not meta.get("acceptance_failed"):
            continue
        key = f"accept:{entry.get('node_id')}"
        if key in seen:
            continue
        seen.add(key)
        errors = meta.get("errors") or []
        hints.append(
            {
                "kind": "acceptance_failure",
                "node_id": entry.get("node_id"),
                "phase": entry.get("phase"),
                "summary": "; ".join(str(e) for e in errors if e),
            }
        )

    for entry in failures[-5:]:
        kind = entry.get("type", "failure")
        if kind in ("execution", "escalation", "agent_trace"):
            continue
        key = f"fail:{kind}:{entry.get('summary', '')[:80]}"
        if key in seen:
            continue
        seen.add(key)
        hints.append({"kind": kind, "summary": entry.get("summary", "")})

    return hints[-15:]


def query_task_logs(
    task_id: str,
    phase: int | None = None,
    node_id: str | None = None,
    log_types: list[str] | None = None,
    limit: int = 100,
    detail: bool = False,
    since: str | None = None,
    offset: int = 0,
    failures_only: bool = False,
) -> dict[str, Any]:
    """Query unified execution timeline for main-agent replanning."""
    init_db()
    allowed = {"eval", "execution", "escalation", "status", "progress", "agent_trace"}
    types = set(log_types) if log_types else allowed

    entries: list[dict[str, Any]] = []

    with connect() as conn:
        if "eval" in types:
            sql = "SELECT phase, data, evaluated_at FROM node_evals WHERE task_id=?"
            params: list[Any] = [task_id]
            if phase is not None:
                sql += " AND phase=?"
                params.append(phase)
            if node_id is not None:
                sql += " AND node_id=?"
                params.append(node_id)
            sql += " ORDER BY id"
            for row in conn.execute(sql, params).fetchall():
                data = _json_loads(row["data"])
                entries.append(
                    {
                        "timestamp": row["evaluated_at"],
                        "type": "eval",
                        "phase": row["phase"],
                        "node_id": data.get("node_id"),
                        "status": "passed" if data.get("passed") else "failed",
                        "summary": _summarize_eval(data),
                        "detail": data if detail else None,
                    }
                )

        if "execution" in types:
            sql = "SELECT phase, node_id, status, data, saved_at FROM node_executions WHERE task_id=?"
            params = [task_id]
            if phase is not None:
                sql += " AND phase=?"
                params.append(phase)
            if node_id is not None:
                sql += " AND node_id=?"
                params.append(node_id)
            sql += " ORDER BY id"
            for row in conn.execute(sql, params).fetchall():
                data = _json_loads(row["data"])
                failure_meta = None
                if row["status"] == "failed":
                    mech = data.get("acceptance_mechanical")
                    if mech and not mech.get("passed"):
                        failed = [r for r in mech.get("results") or [] if not r.get("passed")]
                        failure_meta = {
                            "acceptance_failed": True,
                            "errors": [r.get("error") or r.get("type") for r in failed[:3]],
                        }
                entries.append(
                    {
                        "timestamp": row["saved_at"],
                        "type": "execution",
                        "phase": row["phase"],
                        "node_id": row["node_id"],
                        "status": row["status"],
                        "summary": _summarize_execution(data),
                        "failure_meta": failure_meta,
                        "detail": data if detail else None,
                    }
                )

        if "escalation" in types:
            sql = "SELECT phase, data, escalated_at FROM escalations WHERE task_id=?"
            params = [task_id]
            if phase is not None:
                sql += " AND phase=?"
                params.append(phase)
            sql += " ORDER BY id"
            for row in conn.execute(sql, params).fetchall():
                data = _json_loads(row["data"])
                if node_id is not None and data.get("node_id") != node_id:
                    continue
                entries.append(
                    {
                        "timestamp": row["escalated_at"],
                        "type": "escalation",
                        "phase": row["phase"],
                        "node_id": data.get("node_id"),
                        "status": "escalated",
                        "summary": _summarize_escalation(data),
                        "detail": data if detail else None,
                    }
                )

        if "status" in types or "progress" in types:
            rows = conn.execute(
                "SELECT data, saved_at FROM status_snapshots WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            for row in rows:
                data = _json_loads(row["data"])
                if data.get("kind") == "progress":
                    if "progress" not in types:
                        continue
                    summary = f"progress:{data.get('event')}"
                    if data.get("node_id"):
                        summary += f" {data.get('node_id')}"
                    if data.get("status"):
                        summary += f" ({data.get('status')})"
                    if data.get("message"):
                        summary += f" — {data['message'][:120]}"
                    entries.append(
                        {
                            "timestamp": row["saved_at"],
                            "type": "progress",
                            "phase": data.get("phase"),
                            "node_id": data.get("node_id"),
                            "status": data.get("status") or data.get("event"),
                            "summary": summary,
                            "detail": data if detail else None,
                        }
                    )
                    continue
                if "status" not in types:
                    continue
                entries.append(
                    {
                        "timestamp": row["saved_at"],
                        "type": "status",
                        "phase": data.get("current_phase_index"),
                        "node_id": None,
                        "status": data.get("status"),
                        "summary": data.get("summary") or data.get("status") or "status update",
                        "detail": data if detail else None,
                    }
                )

        if "agent_trace" in types:
            sql = "SELECT phase, node_id, agent_role, step, event_type, data, created_at FROM agent_traces WHERE task_id=?"
            params = [task_id]
            if phase is not None:
                sql += " AND phase=?"
                params.append(phase)
            if node_id is not None:
                sql += " AND node_id=?"
                params.append(node_id)
            sql += " ORDER BY id"
            for row in conn.execute(sql, params).fetchall():
                data = _json_loads(row["data"])
                summary = f"[{row['agent_role']}] step {row['step']}: {row['event_type']}"
                trace_meta = None
                if row["event_type"] == "tool" and isinstance(data, dict):
                    err = data.get("error")
                    if not err and isinstance(data.get("result"), dict):
                        nested = data["result"]
                        err = nested.get("error") or (nested.get("stderr") or "")[:120] or None
                    trace_meta = {
                        "tool": data.get("tool"),
                        "ok": data.get("ok"),
                        "error": err,
                        "timed_out": data.get("timed_out")
                        or (isinstance(data.get("result"), dict) and data["result"].get("timed_out")),
                    }
                    if data.get("ok") is False:
                        summary += f" — {data.get('tool')} failed: {(err or '')[:120]}"
                elif row["event_type"] == "run_end" and isinstance(data, dict):
                    trace_meta = {"status": data.get("status"), "summary": (data.get("summary") or "")[:200]}
                entries.append(
                    {
                        "timestamp": row["created_at"],
                        "type": "agent_trace",
                        "phase": row["phase"],
                        "node_id": row["node_id"],
                        "status": row["event_type"],
                        "summary": summary,
                        "trace_meta": trace_meta,
                        "detail": data if detail else None,
                    }
                )

    entries.sort(key=lambda e: e.get("timestamp") or "")
    if since:
        entries = [e for e in entries if (e.get("timestamp") or "") > since]
    if failures_only:
        entries = [
            e
            for e in entries
            if e.get("status") in ("failed", "escalated", "blocked", "eval_failed", "execute_failed")
            or e.get("type") == "escalation"
            or (e.get("type") == "execution" and e.get("status") == "failed")
            or (e.get("type") == "agent_trace" and (e.get("trace_meta") or {}).get("ok") is False)
        ]
    total = len(entries)
    if offset > 0:
        entries = entries[offset:]
    if limit > 0:
        entries = entries[-limit:]

    failures = [e for e in entries if e.get("status") in ("failed", "escalated") or e.get("type") == "escalation"]
    replan_hints = _build_replan_hints(entries, failures)
    latest_progress = get_latest_progress_event(task_id)

    return {
        "task_id": task_id,
        "filters": {
            "phase": phase,
            "node_id": node_id,
            "log_types": sorted(types),
            "since": since,
            "offset": offset,
            "failures_only": failures_only,
        },
        "total_matched": total,
        "returned": len(entries),
        "latest_progress": latest_progress,
        "replan_hints": replan_hints,
        "entries": entries,
    }
