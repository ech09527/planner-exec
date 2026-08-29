# Planner-Exec (MCP)

主 Agent 规划，MCP 内 **pydantic-ai** agent 验证与执行阶段 DAG。

## 架构

| 层级 | 谁 | 做什么 |
|------|-----|--------|
| 规划 | 主 Agent | `planner_plan` 一次提交 goal/phases/dags |
| 执行 | MCP pydantic-ai | `planner_run_phase` / `planner_run_task` |
| 重规划 | 主 Agent | `planner_query_logs` → 修订 DAG |

数据：默认 `~/.planer-exec/planner.db`；可用 `PE_DATA_DIR` 改目录（见 README）

## 节点 schema v2

```json
{
  "id": "n1",
  "description": "创建 hello.py 打印 hello",
  "acceptance": "hello.py 存在且可运行",
  "acceptance_checks": [
    {"type": "file_exists", "path": "hello.py"},
    {"type": "shell", "command": "python hello.py", "expect_exit": 0}
  ]
}
```

## 环境变量

```bash
export PE_LLM_API_KEY="sk-..."
export PE_LLM_EVAL_MODEL="gpt-4o-mini"
export PE_LLM_EXECUTE_MODEL="gpt-4o-mini"
export PE_AGENT_MAX_STEPS=15          # agent 最大步数
export PE_AGENT_TRACE_MESSAGES=false  # 是否落库完整 message（默认关）
export PE_SHELL_MODE=ai_gate          # off | allowlist | ai_gate | strict
export PE_SHELL_AI_BLOCK=high,blocked # 达到这些级别拒绝执行
export PE_DATA_DIR=~/.planner-exec  # 可选；默认 ~/.planer-exec（历史路径）
export PE_MAX_RESPONSE_CHARS=4000   # MCP/CLI JSON 响应硬上限（默认 4000）
export PE_MCP_DEBUG_TOOLS=0         # 1=注册 eval/execute/next 等调试 tool
export PE_MCP_OBSERVE_TOOLS=0       # 1=注册 list/migrate/show/progress
export PE_CHARS_PER_TOKEN=4         # token 估算（C2 token_ledger 使用）
```

## 推荐工作流

```
planner_plan(plan={goal, goal_confirmed, phases, dags})
→ planner_run(task_id)
→ planner_status / planner_query_logs(since=...)
```

## MCP 工具

### Core（默认 5 个）

| 工具 | 用途 |
|------|------|
| `planner_plan` | 一次提交规划，返回 task_id + summary |
| `planner_run` | 跑全任务；`phase=N` 跑单 phase |
| `planner_status` | 进度 + session + `recommended_next` |
| `planner_replan` | 重规划包 或 应用 patches |
| `planner_query_logs` | 日志（默认 limit=20） |

### Observe（`PE_MCP_OBSERVE_TOOLS=1`）

`planner_init`, `planner_save`, `planner_list`, `planner_show`, `planner_progress`, `planner_migrate`, `planner_token_report`, legacy 别名

### Debug（`PE_MCP_DEBUG_TOOLS=1`）

`planner_eval_*`, `planner_execute_node`, `planner_next_node`

## MCP Prompts

Prompt **不会**自动注入，规划前需 `prompts/get`（或在 Host UI 选择）：

| Prompt | 何时 get |
|--------|----------|
| `plan-design-guide` | 首次 `planner_plan` 前（phase/DAG/node 规则 + 自检清单） |
| `plan-example-calc` | 需要结构参考时（2 phase / 6 nodes 示例 JSON） |
| `replan-guide` | `planner_run` 返回 blocked 后 |

`SERVER_INSTRUCTIONS` 含硬约束与流程；详细指南在上表 Prompt 中。

## Cursor Skill

项目内 [`.cursor/skills/planner-exec/SKILL.md`](.cursor/skills/planner-exec/SKILL.md) 供 Host Agent 规划时加载（工作流、硬约束、blocked 速查）。

## Host 契约

项目根目录 [`CLAUDE.md`](CLAUDE.md) 含主 Agent token 规则。架构详见 [`docs/token-budget.md`](docs/token-budget.md)。

## CLI

```bash
.venv/bin/python -m planner_exec.pe plan --plan-file plan.json --workspace /path/to/ws
.venv/bin/python -m planner_exec.pe run --task-id XXX
.venv/bin/python -m planner_exec.pe status --task-id XXX --increment-poll
.venv/bin/python -m planner_exec.pe replan --task-id XXX
.venv/bin/python -m planner_exec.pe replan --task-id XXX --phase 1 --data '{"patches":[...]}'
```
