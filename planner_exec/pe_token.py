"""Token ledger recording and reporting (C2)."""

from __future__ import annotations

import os
from typing import Any

from . import db
from .pe_budget import response_chars
from .pe_util import utc_now

CHARS_PER_TOKEN = max(1, int(os.environ.get("PE_CHARS_PER_TOKEN", "4")))


def chars_to_tokens(chars: int) -> int:
    return max(0, chars // CHARS_PER_TOKEN)


def record_mcp_response(task_id: str, tool_name: str, payload: dict[str, Any]) -> None:
    chars = response_chars(payload)
    db.record_token_ledger(
        task_id,
        source="mcp_response",
        tool_or_role=tool_name,
        response_chars=chars,
        input_tokens=0,
        output_tokens=chars_to_tokens(chars),
        meta={"chars": chars},
        created_at=utc_now(),
    )


def record_internal_llm_from_usage(
    task_id: str,
    role: str,
    usage: Any,
    *,
    phase: int | None = None,
    node_id: str | None = None,
    model: str | None = None,
) -> None:
    inp = 0
    out = 0
    if isinstance(usage, dict):
        inp = int(usage.get("input_tokens") or usage.get("request_tokens") or 0)
        out = int(usage.get("output_tokens") or usage.get("response_tokens") or 0)
    db.record_token_ledger(
        task_id,
        source="internal_llm",
        tool_or_role=role,
        input_tokens=inp,
        output_tokens=out,
        response_chars=0,
        meta={"phase": phase, "node_id": node_id, "model": model},
        created_at=utc_now(),
    )


def get_token_report(
    task_id: str,
    *,
    host_input_tokens: int | None = None,
    host_output_tokens: int | None = None,
) -> dict[str, Any]:
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT source, tool_or_role, input_tokens, output_tokens, response_chars
            FROM token_ledger WHERE task_id=? ORDER BY id
            """,
            (task_id,),
        ).fetchall()

    mcp_chars = 0
    internal_in = 0
    internal_out = 0
    by_tool: dict[str, int] = {}

    for row in rows:
        source = row["source"]
        tool = row["tool_or_role"]
        if source == "mcp_response":
            mcp_chars += row["response_chars"]
            by_tool[tool] = by_tool.get(tool, 0) + row["response_chars"]
        elif source == "internal_llm":
            internal_in += row["input_tokens"]
            internal_out += row["output_tokens"]

    totals: dict[str, Any] = {
        "mcp_responses_est": chars_to_tokens(mcp_chars),
        "internal_llm": internal_in + internal_out,
        "internal_llm_input": internal_in,
        "internal_llm_output": internal_out,
        "by_tool": {k: chars_to_tokens(v) for k, v in by_tool.items()},
    }
    if host_input_tokens is not None or host_output_tokens is not None:
        totals["host_report"] = {
            "input_tokens": host_input_tokens or 0,
            "output_tokens": host_output_tokens or 0,
        }
        db.record_token_ledger(
            task_id,
            source="host_report",
            tool_or_role="host",
            input_tokens=host_input_tokens or 0,
            output_tokens=host_output_tokens or 0,
            response_chars=0,
            meta={},
            created_at=utc_now(),
        )

    return {
        "task_id": task_id,
        "totals": totals,
        "main_agent_guidance": (
            "internal_llm is NOT billed to you; mcp_responses_est is what returned to your context"
        ),
    }
