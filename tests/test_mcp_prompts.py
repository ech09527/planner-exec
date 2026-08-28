"""MCP prompt registration tests."""

import asyncio

import pytest

from planner_exec.pe_prompts import (
    plan_design_guide_text,
    plan_example_calc_text,
    replan_guide_text,
)
from planner_exec.server import SERVER_INSTRUCTIONS, server


@pytest.mark.parametrize(
    "name",
    ["plan-design-guide", "plan-example-calc", "replan-guide"],
)
def test_list_prompts_includes_registered(name):
    prompts = asyncio.run(server.list_prompts())
    names = {p.name for p in prompts}
    assert name in names


@pytest.mark.parametrize(
    ("name", "snippet"),
    [
        ("plan-design-guide", "reads_from"),
        ("plan-example-calc", "calc/add.py"),
        ("replan-guide", "planner_replan"),
    ],
)
def test_get_prompt_returns_content(name, snippet):
    result = asyncio.run(server.get_prompt(name, {}))
    text = result.messages[0].content.text
    assert snippet in text


def test_prompt_text_helpers_non_empty():
    assert "phase" in plan_design_guide_text().lower()
    assert "calc" in plan_example_calc_text().lower()
    assert "blocked" in replan_guide_text().lower()


def test_server_instructions_reference_prompts():
    assert "plan-design-guide" in SERVER_INSTRUCTIONS
    assert "mechanical_only" in SERVER_INSTRUCTIONS
    assert "reads_from" in SERVER_INSTRUCTIONS
