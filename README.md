# Planner-Exec (MCP)

主 Agent 规划，MCP 内 **pydantic-ai** agent 验证与执行阶段 DAG。

## 架构

| 层级 | 谁 | 做什么 |
|------|-----|--------|
| 规划 | 主 Agent | `planner_init` → `planner_save` 目标/阶段/DAG |
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
planner_init → planner_save(goal/phases/dag)
→ planner_run_task(task_id)           # 或 planner_run_phase
→ planner_status / planner_query_logs(since=...)  # 长跑时轮询；失败时 failures_only
```

## MCP 工具（主要）

**Core（默认注册）：** `planner_init`, `planner_save`, `planner_run_task`, `planner_run_phase`, `planner_status`, `planner_query_logs`, `planner_replan_packet`, `planner_patch_node`, `planner_token_report`, `planner_session_get`, `planner_session_set`

| 工具 | 用途 |
|------|------|
| `planner_init` | 新建任务 |
| `planner_save` | 保存 goal / phases / dag |
| `planner_run_task` | 跑完全部 phase（默认瘦返回 + `_budget`） |
| `planner_run_phase` | 跑单个 phase（默认无 `steps[]`） |
| `planner_status` | 阶段进度 + 最近事件（适合轮询） |
| `planner_query_logs` | 执行日志 + replan_hints；支持 `since` / `failures_only`（默认 limit=20） |
| `planner_replan_packet` | escalation 后最小重规划包（<500 token 目标） |
| `planner_patch_node` | DAG 增量修改（`replace` / `insert_after` / `delete`） |
| `planner_token_report` | MCP 返回 vs 内部 LLM 用量报告 |
| `planner_session_get/set` | Host 会话指针外置 + `recommended_next` |

**Observe（`PE_MCP_OBSERVE_TOOLS=1`）：** `planner_list`, `planner_migrate`, `planner_show`, `planner_progress`

**Debug（`PE_MCP_DEBUG_TOOLS=1`）：** `planner_eval_node`, `planner_execute_node`, `planner_next_node`, `planner_eval_phase`, `planner_eval_status`

## Host 契约

项目根目录 [`CLAUDE.md`](CLAUDE.md) 含主 Agent token 规则。架构详见 [`docs/token-budget.md`](docs/token-budget.md)。

## CLI

```bash
.venv/bin/python -m planner_exec.pe run-task --task-id XXX
.venv/bin/python -m planner_exec.pe status --task-id XXX
.venv/bin/python -m planner_exec.pe query-logs --task-id XXX
.venv/bin/python -m planner_exec.pe replan-packet --task-id XXX
.venv/bin/python -m planner_exec.pe patch-node --task-id XXX --phase 1 --data '{"patches":[...]}'
.venv/bin/python -m planner_exec.pe token-report --task-id XXX
```
