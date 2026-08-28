"""Shell command guard: blocklist + allowlist + optional AI risk classification."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high", "blocked"]
ShellMode = Literal["off", "allowlist", "ai_gate", "strict"]

DEFAULT_MODE: ShellMode = os.environ.get("PE_SHELL_MODE", "ai_gate")  # type: ignore[assignment]
AI_BLOCK_LEVELS = frozenset(
    lvl.strip().lower()
    for lvl in os.environ.get("PE_SHELL_AI_BLOCK", "high,blocked").split(",")
    if lvl.strip()
)


class CommandRisk(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    summary: str = ""


@dataclass
class ShellGuardResult:
    allowed: bool
    level: RiskLevel
    source: Literal["blocklist", "allowlist", "ai", "mode", "error"]
    reasons: list[str] = field(default_factory=list)
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "level": self.level,
            "source": self.source,
            "reasons": self.reasons,
            "command": self.command,
        }

    @property
    def error_message(self) -> str:
        detail = "; ".join(self.reasons) or self.level
        return f"shell guard blocked ({self.source}/{self.level}): {detail}"


_BLOCK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("destructive_rm", re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|-[a-zA-Z]*r[a-zA-Z]*f|--recursive|--no-preserve-root)", re.I)),
    ("pipe_to_shell", re.compile(r"\|\s*(ba)?sh\b", re.I)),
    ("curl_pipe_shell", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh\b", re.I)),
    ("redirect_system", re.compile(r">\s*/(etc|dev|proc|sys|var/run)\b", re.I)),
    ("chmod_wide", re.compile(r"\bchmod\s+(-R\s+)?777\b", re.I)),
    ("disk_write", re.compile(r"\b(mkfs|dd)\b", re.I)),
    ("fork_bomb", re.compile(r":\(\)\s*\{", re.I)),
    ("eval_exec", re.compile(r"\b(eval|exec)\s+", re.I)),
    ("sudo", re.compile(r"\bsudo\b", re.I)),
    ("network_exfil", re.compile(r"\b(nc|ncat|netcat)\b", re.I)),
]

_ALLOWLIST_RE = re.compile(
    r"^("
    r"pytest\b|"
    r"python3?\s+(-m\s+\S+|\S+\.py\b)|"
    r"pip3?\s+(install|show|list)\b|"
    r"npm\s+(run|test|ci|install)\b|"
    r"pnpm\s+(run|test|install)\b|"
    r"yarn\s+(run|test|install)\b|"
    r"uv\s+run\b|"
    r"cargo\s+(test|build|check)\b|"
    r"go\s+(test|build|vet)\b|"
    r"make\b|"
    r"cmake\b|"
    r"git\s+(status|diff|log|show|rev-parse|branch)\b|"
    r"(ls|cat|head|tail|wc|grep|rg|find|test|echo|true|false|pwd|which)\b"
    r")",
    re.I,
)


def _normalize_mode(raw: str | None) -> ShellMode:
    mode = (raw or DEFAULT_MODE).strip().lower()
    if mode in ("off", "allowlist", "ai_gate", "strict"):
        return mode  # type: ignore[return-value]
    return "ai_gate"


def _check_blocklist(command: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in _BLOCK_PATTERNS:
        if pattern.search(command):
            hits.append(name)
    return hits


def _check_allowlist(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    return bool(_ALLOWLIST_RE.match(stripped))


def _level_blocks(level: RiskLevel) -> bool:
    return level in AI_BLOCK_LEVELS


def _ai_classify_risk(command: str) -> CommandRisk:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from .pe_llm import build_pe_model, load_llm_config

    cfg = load_llm_config("eval")
    if not cfg["api_key"]:
        return CommandRisk(
            level="high",
            reasons=["no LLM API key for shell risk classification"],
            summary="cannot classify — fail closed",
        )

    agent: Agent[None, CommandRisk] = Agent(
        OpenAIChatModel("gpt-4o-mini", provider=OpenAIProvider(api_key="placeholder")),
        output_type=CommandRisk,
        system_prompt=(
            "你是 shell 命令风险评估器。任务是在开发 workspace 内执行构建/测试命令。"
            "只输出结构化风险等级，不要执行命令。\n"
            "low: 常规构建测试 (pytest, npm test, python script)\n"
            "medium: 有网络或安装但目的明确\n"
            "high: 删除、权限变更、下载执行、访问系统路径、模糊危险操作\n"
            "blocked: 明显恶意或不可逆破坏"
        ),
    )
    try:
        result = agent.run_sync(
            f"评估以下命令风险（仅在项目 workspace 内执行）：\n\n{command}",
            model=build_pe_model(cfg),
        )
        return result.output
    except Exception as exc:
        return CommandRisk(
            level="high",
            reasons=[f"risk classifier failed: {exc}"],
            summary="classifier error — fail closed",
        )


def guard_command(
    command: str,
    mode: str | None = None,
    *,
    use_ai: bool | None = None,
) -> ShellGuardResult:
    """Return whether a shell command may run under the configured policy."""
    cmd = (command or "").strip()
    if not cmd:
        return ShellGuardResult(
            allowed=False,
            level="blocked",
            source="error",
            reasons=["empty command"],
            command=cmd,
        )

    resolved_mode = _normalize_mode(mode)
    block_hits = _check_blocklist(cmd)
    if block_hits:
        return ShellGuardResult(
            allowed=False,
            level="blocked",
            source="blocklist",
            reasons=[f"matched block rule: {h}" for h in block_hits],
            command=cmd,
        )

    if resolved_mode == "off":
        return ShellGuardResult(allowed=True, level="low", source="mode", reasons=["PE_SHELL_MODE=off"], command=cmd)

    if _check_allowlist(cmd):
        return ShellGuardResult(allowed=True, level="low", source="allowlist", reasons=["allowlisted"], command=cmd)

    if resolved_mode in ("allowlist", "strict"):
        return ShellGuardResult(
            allowed=False,
            level="high",
            source="mode",
            reasons=[f"command not on allowlist (mode={resolved_mode})"],
            command=cmd,
        )

    # ai_gate
    should_ai = use_ai if use_ai is not None else True
    if not should_ai:
        return ShellGuardResult(
            allowed=False,
            level="high",
            source="mode",
            reasons=["not allowlisted and AI gate disabled"],
            command=cmd,
        )

    risk = _ai_classify_risk(cmd)
    allowed = not _level_blocks(risk.level)
    return ShellGuardResult(
        allowed=allowed,
        level=risk.level,
        source="ai",
        reasons=risk.reasons or ([risk.summary] if risk.summary else []),
        command=cmd,
    )


def run_guarded_shell(
    workspace: str | None,
    command: str,
    timeout: int = 120,
    *,
    dry_run: bool = False,
    shell_mode: str | None = None,
) -> dict[str, Any]:
    """Run a command after guard checks. Returns same shape as pe_actions run_shell result."""
    root = Path(workspace).resolve() if workspace else None
    if not root or not root.exists():
        return {"ok": False, "type": "run_shell", "command": command, "error": f"workspace not found: {workspace}"}

    guard = guard_command(command, mode=shell_mode)
    if not guard.allowed:
        return {
            "ok": False,
            "type": "run_shell",
            "command": command,
            "error": guard.error_message,
            "guard": guard.to_dict(),
        }

    if dry_run:
        return {
            "ok": True,
            "type": "run_shell",
            "command": command,
            "dry_run": True,
            "guard": guard.to_dict(),
        }

    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "type": "run_shell",
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
        "guard": guard.to_dict(),
    }
