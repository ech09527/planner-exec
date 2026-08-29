"""MCP prompt templates for planner-exec (planning guides, examples)."""

from __future__ import annotations

PLAN_EXAMPLE_CALC: dict = {
    "goal": "构建 calc 计算器包（2 phase / 6 nodes）",
    "goal_confirmed": {
        "goal": "workspace 内实现 calc 包：add/mul、__init__、CLI、unittest，最终集成验收",
        "success_criteria": [
            "phase1 产出 calc/add.py calc/mul.py calc/__init__.py",
            "phase2 产出 calc/cli.py tests/test_calc.py",
            "最终 python3 -m calc.cli add 2 3 输出 5 且 unittest 通过",
        ],
        "resources": ["python3"],
        "constraints": ["仅 workspace 内文件", "不安装第三方包"],
        "assumptions": ["各节点执行阶段才写文件", "集成测试集中在最后一个节点"],
        "open_questions": [],
    },
    "phases": {
        "phases": [
            {
                "id": "p1",
                "title": "核心库",
                "objective": "add/mul 与包初始化",
                "inputs": [],
                "outputs": ["calc/add.py", "calc/mul.py", "calc/__init__.py"],
                "done_definition": "三个文件存在且可 import",
            },
            {
                "id": "p2",
                "title": "CLI 与测试",
                "objective": "CLI + unittest + 集成验收",
                "inputs": ["calc 包"],
                "outputs": ["calc/cli.py", "tests/test_calc.py"],
                "done_definition": "CLI 与 unittest 通过",
            },
        ]
    },
    "dags": [
        {
            "phase": 1,
            "nodes": [
                {
                    "id": "n1",
                    "description": "写 calc/add.py：def add(a,b): return a+b。写完即完成。",
                    "acceptance": "calc/add.py 存在",
                    "acceptance_checks": [{"type": "file_exists", "path": "calc/add.py"}],
                },
                {
                    "id": "n2",
                    "description": "写 calc/mul.py：def mul(a,b): return a*b。写完即完成。",
                    "acceptance": "calc/mul.py 存在",
                    "acceptance_checks": [{"type": "file_exists", "path": "calc/mul.py"}],
                },
                {
                    "id": "n3",
                    "description": "写 calc/__init__.py，from .add import add; from .mul import mul。",
                    "acceptance": "calc/__init__.py 存在",
                    "acceptance_checks": [{"type": "file_exists", "path": "calc/__init__.py"}],
                    "reads_from": ["n1", "n2"],
                },
            ],
        },
        {
            "phase": 2,
            "nodes": [
                {
                    "id": "n4",
                    "description": "写 calc/cli.py，支持 python -m calc.cli add A B / mul A B，打印整数结果。",
                    "acceptance": "calc/cli.py 存在",
                    "acceptance_checks": [{"type": "file_exists", "path": "calc/cli.py"}],
                },
                {
                    "id": "n5",
                    "description": "写 tests/test_calc.py，unittest 测试 add/mul 至少各 1 个用例。",
                    "acceptance": "tests/test_calc.py 存在",
                    "acceptance_checks": [{"type": "file_exists", "path": "tests/test_calc.py"}],
                },
                {
                    "id": "n6",
                    "description": "集成验收：确认 import、CLI、unittest 均可运行（只跑命令不写文件）。",
                    "acceptance": "import/CLI/unittest 全通过",
                    "acceptance_checks": [
                        {
                            "type": "shell",
                            "command": 'python3 -c "from calc import add, mul; assert add(2,3)==5 and mul(2,3)==6"',
                            "expect_exit": 0,
                        },
                        {"type": "shell", "command": "python3 -m calc.cli add 2 3 | grep -q 5", "expect_exit": 0},
                        {
                            "type": "shell",
                            "command": "python3 -m unittest discover -s tests -p 'test_*.py' -q",
                            "expect_exit": 0,
                        },
                    ],
                    "reads_from": ["n4", "n5"],
                },
            ],
        },
    ],
}


def plan_design_guide_text() -> str:
    return """
# planner_plan 规划指南

你负责组 plan JSON，再调用 planner_plan。本 MCP 内 cheap 模型按 phase DAG 在 workspace 执行。

## 一、phase 怎么拆
- 每个 phase = 一个可独立验收的里程碑，必须有清晰 done_definition
- 用 phases[].inputs / outputs 串接阶段（后 phase 的 inputs 写前 phase 产出）
- 建议 2–4 个 phase；单 phase 内 2–6 个 node；过大则拆 phase 或拆 node
- 每个 phase 必填：id, title, objective, inputs, outputs, done_definition

## 二、DAG 怎么设计（每个 phase 一张 DAG）
- dags[] 每项对应一个 phase 编号；一张 DAG 只服务一个 phase
- reads_from 仅引用**同一 phase** 内上游 node id
- **禁止** cross-phase reads_from；跨 phase 依赖只写在 phases.inputs/outputs
- 推荐拓扑：并行写文件 → 汇合（如 __init__）→ 最后一节点做 shell/unittest 集成验收
- node id 稳定短名（n1, n2…）；reads_from 指向的 id 必须存在于同 DAG

## 三、node 怎么写（schema v2）
每个节点必填：id, description, acceptance, acceptance_checks[]

**description**
- 写「执行阶段将要做什么」，不要写 workspace 里文件是否已存在
- 不要要求 execute 前 workspace 已有产物（eval 可能读目录）

**acceptance**
- 一句话、可机械验证，与 acceptance_checks 一致

**acceptance_checks**（仅三种）
- file_exists: {"type":"file_exists","path":"相对 workspace 路径"}
- shell: {"type":"shell","command":"...","expect_exit":0}
- file_contains: {"type":"file_contains","path":"...","contains":"..."}

**reads_from**
- 同 phase 上游 node id 列表；仅在上游完成后执行

**验收策略**
- 中间节点：1–2 个 check，优先 file_exists
- 复杂 shell / unittest：放在该 phase **最后一个节点**

## 四、plan 包结构
```json
{
  "goal": "一句话",
  "goal_confirmed": {
    "goal", "success_criteria", "resources", "constraints", "assumptions", "open_questions"
  },
  "phases": { "phases": [{ "id", "title", "objective", "inputs", "outputs", "done_definition" }] },
  "dags": [{ "phase": 1, "nodes": [...] }]
}
```

## 五、提交前自检
□ 每个 phase 有 done_definition？
□ 每个 node 有 description + acceptance + ≥1 check？
□ reads_from 都在同一 phase？
□ 每个 phase 最后一节点负责集成验收？
□ description 未描述「文件已存在/缺失」？

## 六、提交后
- planner_plan 只记 task_id + summary；勿在对话复述 nodes[] 全文
- planner_run 必先 LLM eval 再 execute（不可跳过）
- blocked：planner_replan → patch → run；勿整包重 plan
""".strip()


def plan_example_calc_text() -> str:
    import json

    return (
        "# plan 示例：calc 包（2 phase / 6 nodes）\n\n"
        "结构参考，勿照抄 goal；按你的 workspace 任务改写。\n\n"
        f"```json\n{json.dumps(PLAN_EXAMPLE_CALC, ensure_ascii=False, indent=2)}\n```"
    )


def replan_guide_text() -> str:
    return """
# blocked 后重规划指南

## 何时 replan
- planner_run 返回 status=blocked
- planner_status.recommended_next 为 planner_replan
- 不要整包重 planner_plan（除非 goal 本身错了）

## 步骤
1. planner_replan(task_id) — 无 patches，拿 replan_packet
2. 读 blocked / failure / suggested_patches / context_digest
3. planner_replan(task_id, phase=N, patches=[...]) — 应用修订
4. planner_run(task_id) — 继续执行

## patch 操作
- replace: {"op":"replace","node_id":"n1","fields":{"description":"...","acceptance":"..."}}
- insert_after: {"op":"insert_after","after":"n1","node":{完整 node 对象}}
- delete: {"op":"delete","node_id":"n1"}

## 日志
- planner_query_logs(task_id, failures_only=true, limit=20)
- 禁止 detail=true（token 爆炸）

## 常见修复
- eval 因「文件不存在」失败 → 改 description 为将来动作（eval 在 execute 前，产物未创建是正常的）
- execute 步数耗尽 → 简化单节点 checks；集成放最后节点；description 写明「写完即停」
- cross-phase reads_from → 删掉，改 phases.inputs 描述依赖
""".strip()
