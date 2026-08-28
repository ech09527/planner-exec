"""LLM client — cheap worker tier for eval + execute (pydantic-ai agents)."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx

Role = Literal["eval", "execute"]


def load_llm_config(role: Role = "eval") -> dict[str, str]:
    provider = os.environ.get("PE_LLM_PROVIDER", os.environ.get("LLM_PROVIDER", "openai"))
    api_key = (
        os.environ.get("PE_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    base_url = os.environ.get(
        "PE_LLM_BASE_URL",
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    ).rstrip("/")

    if role == "execute":
        model = (
            os.environ.get("PE_LLM_EXECUTE_MODEL")
            or os.environ.get("PE_LLM_WORKER_MODEL")
            or os.environ.get("PE_LLM_MODEL")
            or "gpt-4o-mini"
        )
    else:
        model = (
            os.environ.get("PE_LLM_EVAL_MODEL")
            or os.environ.get("PE_LLM_WORKER_MODEL")
            or os.environ.get("PE_LLM_MODEL")
            or "gpt-4o-mini"
        )

    return {
        "provider": provider.lower(),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "role": role,
    }


def llm_available() -> bool:
    return bool(load_llm_config()["api_key"])


def _needs_openai_response_compat(base_url: str) -> bool:
    """Some OpenAI-compatible proxies omit required fields (e.g. object=chat.completion)."""
    if os.environ.get("PE_LLM_OPENAI_COMPAT", "").lower() in ("1", "true", "yes"):
        return True
    host = base_url.lower()
    return "nocsdn.com" in host or "copilot-api" in host


async def _patch_openai_chat_completion_response(response: httpx.Response) -> None:
    if "/chat/completions" not in str(response.url):
        return
    await response.aread()
    try:
        data = json.loads(response.content)
    except json.JSONDecodeError:
        return
    if data.get("object") is None and "choices" in data:
        data["object"] = "chat.completion"
        response._content = json.dumps(data).encode("utf-8")


def _openai_compat_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(event_hooks={"response": [_patch_openai_chat_completion_response]})


def build_pe_model(cfg: dict[str, str]):
    """Build a pydantic-ai model from planner-exec LLM config."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if cfg["provider"] == "anthropic":
        try:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                cfg["model"],
                provider=AnthropicProvider(api_key=cfg["api_key"]),
            )
        except Exception:
            pass

    api_key = cfg["api_key"] or "placeholder"
    provider_kwargs: dict[str, Any] = {
        "base_url": cfg["base_url"],
        "api_key": api_key,
    }
    if _needs_openai_response_compat(cfg["base_url"]):
        provider_kwargs["http_client"] = _openai_compat_http_client()
    return OpenAIChatModel(
        cfg["model"],
        provider=OpenAIProvider(**provider_kwargs),
    )


def evaluate_node_with_llm(context: dict[str, Any]) -> dict[str, Any]:
    from .pe_agent import evaluate_node_with_agent

    return evaluate_node_with_agent(context)


def execute_node_with_llm(context: dict[str, Any]) -> dict[str, Any]:
    from .pe_agent import execute_node_with_agent

    return execute_node_with_agent(context)
