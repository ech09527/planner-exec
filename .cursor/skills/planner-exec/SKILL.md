---
name: planner-exec
description: >-
  Plan and execute multi-phase DAG tasks via the planner-exec MCP
  (planner_plan, planner_run, planner_replan). Use when the user asks to plan
  with planner-exec, build a phase DAG, run a planner task, recover from
  blocked status, or mentions planner_plan / planner_run / MCP planning.
---

# Planner-Exec

主 Agent 规划，MCP 内 cheap LLM **先 eval 再 execute** phase DAG。

## 何时用

- 用户要求用 planner-exec / MCP 规划并执行
- 需要组 `planner_plan` JSON 或处理 `blocked` 重规划

## 工作流

```
1. prompts/get → plan-design-guide   （首次规划必做）
2. 可选 get → plan-example-calc
3. planner_plan(plan={goal, goal_confirmed, phases, dags})
4. 只记 task_id + summary；勿复述 nodes[]
5. planner_run(task_id)              （不可跳过 eval）
6. blocked → get replan-guide → planner_replan → patches → run
```

轮询用 `planner_status`；日志用 `planner_query_logs(limit=20)`，**禁止 `detail=true`**。

## 硬约束

| 规则 | 说明 |
|------|------|
| 一次提交 | 新任务只用 `planner_plan`，禁止 init+save 分步 |
| reads_from | 仅同 phase 内 node id；跨 phase 用 `phases.inputs/outputs` |
| description | 写「执行阶段将做什么」；勿写 workspace 文件是否已存在 |
| 验收 | 中间节点优先 `file_exists`；shell/unittest 放 phase 最后一节点 |
| run | 始终机械检查 + LLM eval，再 execute；无跳过开关 |
| blocked | `planner_replan`，勿整包重 plan |

## plan 包骨架

```json
{
  "goal": "一句话",
  "goal_confirmed": {
    "goal": "...",
    "success_criteria": [],
    "resources": [],
    "constraints": [],
    "assumptions": [],
    "open_questions": []
  },
  "phases": {
    "phases": [
      {
        "id": "p1",
        "title": "...",
        "objective": "...",
        "inputs": [],
        "outputs": [],
        "done_definition": "..."
      }
    ]
  },
  "dags": [
    {
      "phase": 1,
      "nodes": [
        {
          "id": "n1",
          "description": "写 foo.py：…。写完即完成。",
          "acceptance": "foo.py 存在",
          "acceptance_checks": [{"type": "file_exists", "path": "foo.py"}]
        }
      ]
    }
  ]
}
```

`acceptance_checks` 仅：`file_exists` | `shell` | `file_contains`。

## MCP Prompts（按需 get，不会自动注入）

| Prompt | 何时 |
|--------|------|
| `plan-design-guide` | 首次 `planner_plan` 前 |
| `plan-example-calc` | 需要 2 phase / 6 nodes 结构参考 |
| `replan-guide` | `planner_run` 返回 blocked |

## blocked 速查

1. `planner_replan(task_id)` → 读 `failure` / `suggested_patches`
2. `planner_query_logs(task_id, failures_only=true, limit=20)`
3. `planner_replan(task_id, phase=N, patches=[...])`
4. `planner_run(task_id)`

常见原因：description 写成「文件已存在」；cross-phase `reads_from`；单节点过大导致 execute 工具循环。

## Token

只保留 `task_id` 与 summary；详见仓库根目录 `CLAUDE.md`。
