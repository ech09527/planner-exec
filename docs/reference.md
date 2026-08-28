# Planner-Exec 数据格式（MCP + SQLite）

存储目录：默认 `~/.planer-exec/`（历史拼写）；可通过环境变量 `PE_DATA_DIR` 覆盖（例如 `~/.planner-exec`）。  
数据库：`planner.db`（SQLite WAL）  
通过 MCP 工具 `planner_*` 读写，无全局 `current`。

## SQLite 表

| 表 | 用途 |
|----|------|
| `tasks` | 任务元数据（task_id、status、workspace、重试上限） |
| `artifacts` | goal-raw、goal-confirmed、phases 等 JSON 文档 |
| `phase_dags` | 每 phase 的 DAG（含 dag_revision） |
| `node_evals` | 节点验证结果（机械 + LLM） |
| `node_executions` | 节点执行记录 |
| `escalations` | 需主 Agent 重规划的升级事件 |
| `status_snapshots` | 状态快照 + **progress 事件**（run_task 进度） |
| `agent_traces` | pydantic-ai 内部 agent 工具调用轨迹 |

## 节点 schema v2

```json
{
  "id": "n1",
  "description": "创建 hello.py 打印 hello",
  "acceptance": "hello.py 存在且可运行",
  "reads_from": [],
  "acceptance_checks": [
    {"type": "file_exists", "path": "hello.py"},
    {"type": "shell", "command": "python hello.py", "expect_exit": 0}
  ]
}
```

## 单节点评估结果（node_evals.data）

```json
{
  "task_id": "260801-xxx",
  "phase": 1,
  "node_id": "n1",
  "iteration": 1,
  "passed": false,
  "mechanical": {"passed": true, "issues": [], "schema": "v2"},
  "llm": {
    "passed": false,
    "issues": [{"severity": "blocker", "type": "ambiguity", "message": "验收标准不可量化"}],
    "suggestions": ["改为可检查的命令与 exit code"]
  },
  "issues": [],
  "blocker_count": 1
}
```

## 节点执行记录（node_executions.data）

```json
{
  "node_id": "n1",
  "status": "success",
  "outputs": {},
  "acceptance_mechanical": {"passed": true, "skipped": false, "results": []},
  "actions": [],
  "executor": "pydantic-ai",
  "model": "gpt-4o-mini"
}
```

## Progress 事件（status_snapshots，kind=progress）

`run_task` / `run_phase` 执行时写入，供 `planner_query_logs(log_types=progress)` 或 `since=` 轮询：

| event | 含义 |
|-------|------|
| `task_start` / `task_done` | 全任务 |
| `phase_start` / `phase_eval_done` / `phase_done` | 阶段 |
| `node_start` / `node_done` | 单节点 |
| `blocked` / `phase_blocked` | 需重规划 |

## 遗留 JSON 目录（仅 migrate 用）

```
~/.planer-exec/tasks/<task_id>/   # 旧版文件存储，planner_migrate 导入 SQLite
```

## 查询日志

```text
planner_query_logs(task_id, since=..., failures_only=true)
→ entries: eval | execution | escalation | progress | agent_trace
→ replan_hints: tool_failure | execution_failure | acceptance_failure | escalation
→ latest_progress: 最近一次 progress 事件
```

**注意**：无全局 `current` 指针；多 Agent 并行时各自持有 `task_id`。
