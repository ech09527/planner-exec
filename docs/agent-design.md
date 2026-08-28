# MCP 内部 Agent 设计

## 框架：**pydantic-ai**

- **验证 agent**：只读工具 → `ValidateResult`
- **执行 agent**：读写工具 → `ExecuteResult`
- 每步 `agent_traces`；主 Agent 用 `planner_query_logs` 查 `replan_hints`

## 节点 schema v2

```json
{
  "id": "n2",
  "description": "自然语言任务描述",
  "reads_from": ["n1"],
  "acceptance": "可验证的完成标准",
  "acceptance_checks": [
    {"type": "file_exists", "path": "src/foo.py"},
    {"type": "shell", "command": "pytest -q", "expect_exit": 0},
    {"type": "file_contains", "path": "README.md", "contains": "install"}
  ]
}
```

- `depends_on` 与 `reads_from` 合并用于拓扑排序
- v2 节点：`id` + `description` + `acceptance` + 可选 `reads_from` / `acceptance_checks`

## 执行后机械验收

`pe_acceptance.check_node_acceptance()` 在 LLM 返回后运行 `acceptance_checks`。
未定义 checks 时沿用 LLM 的 `status`。

## Shell 安全（`pe_shell.py`）

```text
run_shell / acceptance_checks.shell
  → blocklist（rm -rf、pipe to sh、sudo…）
  → allowlist（pytest、npm test、python -m…）→ 直接执行
  → ai_gate：其余命令 → pydantic-ai CommandRisk 评估
  → high/blocked → 拒绝 + 写入 guard 信息到 trace/execution
```

环境变量：`PE_SHELL_MODE`（默认 `ai_gate`）、`PE_SHELL_AI_BLOCK`（默认 `high,blocked`）

## 重规划

```text
planner_query_logs(task_id, phase=1)
→ replan_hints: tool_failure | execution_failure | acceptance_failure | escalation
→ latest_progress: 最近 run_task 事件（phase_start / node_done / blocked…）
```

轮询：`planner_query_logs(task_id, since=<上次时间戳>, log_types=progress)`
失败排查：`failures_only=true`
