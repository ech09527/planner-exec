# Planner-Exec Token 规则

## Core 工具（5 个）

| 工具 | 用途 |
|------|------|
| `planner_plan` | **一次**提交 goal + phases + dags，返回 task_id + summary |
| `planner_run` | 跑任务；`phase=N` 只跑单 phase |
| `planner_status` | 进度 + session + `recommended_next` |
| `planner_replan` | 无 patches → 重规划包；有 patches → 改 DAG |
| `planner_query_logs` | 日志（默认 limit=20） |

## 推荐流程

```
planner_plan(plan={goal, goal_confirmed, phases, dags})
→ planner_run(task_id)
→ [blocked] planner_replan → planner_replan(patches=...) → planner_run
```

**禁止**对新任务使用 init+save 分步（除非 Observe 层增量修改）。

## plan 包结构

```json
{
  "goal": "一句话目标",
  "goal_confirmed": { "goal", "success_criteria", "resources", "constraints", "assumptions", "open_questions": [] },
  "phases": { "phases": [{ "id", "title", "objective", "inputs", "outputs", "done_definition" }] },
  "dags": [{ "phase": 1, "nodes": [{ "id", "description", "acceptance", "acceptance_checks": [...] }] }]
}
```

`planner_plan` 只返回 summary，**不要在对话里复述 nodes[]**。

## 其他规则

1. 只记 `task_id` + summary 一行。
2. 轮询用 `planner_status`；`query_logs` 禁止 `detail=true`。
3. 失败走 `planner_replan`，不要整包重 `plan`。
4. `planner_run` 必先 LLM eval 再 execute，不可跳过。
5. `init` / `save` 在 `PE_MCP_OBSERVE_TOOLS=1` 下可用。
6. Host 规划手册：`.cursor/skills/planner-exec/SKILL.md`。

详见 [`docs/token-budget.md`](docs/token-budget.md)。
