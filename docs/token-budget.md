# Token Budget Layer

Planner-exec 通过三层机制降低主 Agent 的 MCP 返回 token 成本。

## 架构

```
主 Agent (Cursor)
  │ 只持有 task_id + session 指针
  ▼
MCP Core Tools
  ├── run_task / run_phase   → 瘦返回 + _budget
  ├── replan_packet          → 失败时 <500 token 重规划包
  ├── patch_node             → 增量改 DAG
  ├── query_logs             → 默认 limit=20
  └── token_report           → 用量分离报告
  ▼
SQLite (planner.db)
  └── 完整 phases/steps/logs（不进主 Agent 上下文）
```

## C1：响应硬上限

- 环境变量 `PE_MAX_RESPONSE_CHARS`（默认 4000）
- 所有 budget 工具经 `emit_json_response()` 输出
- 截断时 `_budget.fetch_hints` 指引下一步拉取

## C2：重规划 + 度量

### replan_packet

`status=blocked` 或 escalation 后调用。包含：

- `blocked` — phase / node_id / reason / node_summary
- `failure` — kind / errors / last_tool_error
- `suggested_patches` — 规则模板（非 LLM）
- `context_digest` — goal / phase / upstream_done

超标降级顺序：`suggested_patches` → `context_digest` → `failure.errors`

### patch_node

| op | 说明 |
|----|------|
| `replace` | 合并字段，保留 node id |
| `insert_after` | 在 `after` 节点后插入新节点（需完整 id/description/acceptance） |
| `delete` | 删除节点；若有依赖引用则拒绝；不能删最后一个节点 |

`dag_revision` 为内容 SHA256 哈希，patch 后自动更新。

### token_ledger

| source | 记录时机 |
|--------|----------|
| `mcp_response` | budget 工具返回时（chars/4 估算） |
| `internal_llm` | pydantic-ai agent run_end |
| `host_report` | `token_report(host_input_tokens=...)` 可选回传 |

## C3：Host 契约

见项目根 [`CLAUDE.md`](../CLAUDE.md)。

### session 指针

```
planner_session_set(task_id, last_since=..., last_status_line=..., increment_poll=true)
planner_session_get(task_id) → recommended_next
```

`recommended_next` 状态机：

- blocked / escalation → `planner_replan_packet`
- running + poll < 60 → `planner_status`
- completed → `planner_token_report`

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PE_MAX_RESPONSE_CHARS` | 4000 | MCP JSON 硬上限 |
| `PE_MCP_DEBUG_TOOLS` | 0 | 调试 tool |
| `PE_MCP_OBSERVE_TOOLS` | 0 | list/show/progress |
| `PE_CHARS_PER_TOKEN` | 4 | token 估算 |

## 验收指标

| 指标 | 目标 |
|------|------|
| Happy path MCP 累计 | < 2k token / task |
| 单次 MCP 返回 | < 500 token |
| replan_packet | < 500 token |
| run_task 默认返回 | < 1k token |

用 `planner_token_report` 查看 `mcp_responses_est` vs `internal_llm`。
