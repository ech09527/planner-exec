# Planner-Exec Token 规则

主 Agent 使用 planner-exec MCP 时请遵守以下规则，以降低对话 token 成本。

## 会话状态（只记指针，不记历史）

每轮在脑中或 `planner_session_set` 中维护：

```
ACTIVE_TASK=<task_id>
LAST_SINCE=<ISO 时间戳>
LAST_STATUS_LINE=<planner_status 返回的一行摘要>
```

所有执行细节在 SQLite；需要时用 MCP 按需拉取。

## 工具使用

1. 规划后只记 `task_id`，不要把 DAG 全文留在对话里复述。
2. 长跑用 `planner_run_task`，不要逐节点调用 `planner_execute_node`。
3. 进度轮询用 `planner_status`；禁止 `planner_show` / `planner_progress`（除非 `PE_MCP_OBSERVE_TOOLS=1`）。
4. 需要增量事件时，维护 `since` 时间戳，调 `planner_query_logs(since=..., log_types=progress)`。
5. **失败时先调 `planner_replan_packet`**；再用 `planner_patch_node` 改 DAG；`planner_query_logs` 禁止 `detail=true`。
6. 改 DAG 用 `planner_patch_node`，不要整包 `planner_save(dag)` 重传（除非大改）。
7. escalation 后开新 turn 或 compact，避免带着大量旧 status 重规划。
8. 任务完成后可调 `planner_token_report` 查看 MCP 返回侧估算用量。

## 推荐流程

```
planner_init
→ planner_save(goal / phases / dag)
→ planner_run_task
→ [blocked] planner_replan_packet → planner_patch_node → planner_run_task
→ planner_token_report
```

## 响应预算

MCP 响应默认有 `PE_MAX_RESPONSE_CHARS` 硬上限。若返回含 `_budget.truncated=true`，按 `fetch_hints` 指引拉取详情，不要重复请求同一超大 payload。

`run_task` 返回 `status=blocked` 时，应调用 `planner_replan_packet`（`_budget.fetch_hints` 也会提示）。

完整架构说明见 [`docs/token-budget.md`](docs/token-budget.md)。
