# Agent trace 元数据扩展实施计划（token / 耗时 / 模型配置）

> **状态**：已实施（2026-08-16）。实施顺序：后端捕获 → 迁移 → 前端展示 → 测试回归。
> **目标**：给现有 agent_runs trace 补上 LangSmith 能自动捕获、但当前手工插桩缺失的**元数据层**——
> 每步 token 用量、每步耗时、模型/配置快照，并在 Agent Runs 前端流程视图/时间线展示。**不接 LangSmith 云、不存对话内容**。
> **决策**：只做 A 方案（元数据层）；B（prompt/工具全量快照）、C（parent/child run 树）明确不做。

---

## 1. 背景与动因

当前 Agent Runs trace（ADR-0017）是**手工插桩、只存元数据**：6 种条目（thought / tool_call /
tool_result / answer / convergence_override / reflection）记录"这条发生了什么"，但**不记录**：

- 每步花了多少 token（输入/输出/思考）
- 每步耗时（模型调用 / 工具执行）
- 跑的时候用的是哪个模型、什么配置

对照评估（2026-08-15）：LangSmith/Studio 能自动捕获这些（prompt、tool args、token/latency
指标），但引入 LangSmith 的代价（云 tracing 发内部对话、两套 eval 体系、与"数据本地化"叙事冲突）
大于收益。**正确补深度的方式是给现有 trace 加字段，而不是接云。**

**为什么元数据层安全**：token 数、毫秒、模型名都是**数字与配置**，不涉及对话内容，符合
"agent trace 默认只存元数据"的既有哲学（与 `AGENT_TRACE_INCLUDE_REASONING` 脱敏不冲突）。

## 2. 范围（只做 A）

| 捕获项 | 粒度 | 来源 |
|---|---|---|
| `tokens`：input / output / reasoning | 每轮模型调用 | `AIMessage.usage_metadata`（已实测 DeepSeek 返回：`input_tokens/output_tokens/total_tokens` + `output_token_details.reasoning`） |
| `ms`：调用耗时 | 每轮模型调用 / 每个工具执行 | 时间戳差 |
| 模型/配置快照 | 每 run | `settings`（model / temperature / thinking / max_tokens） |
| 汇总：run 总 token + 估算成本 | 每 run | 各步 token 求和 × DeepSeek 单价 |

**不做**（B/C）：每轮完整 prompt、工具全量输出、parent/child run 树——分别因隐私、trace 体积、
数据模型重构。

## 3. 后端捕获点（codeaware-py/app/ai/agent/agent_graph.py）

### 3.1 agent_node（每轮模型调用）
- **验证点**：`accumulated = accumulated + chunk` 聚合后，`accumulated.usage_metadata` 是否保留
  （langchain-core 的 AIMessageChunk 加法应取末 chunk 的 usage；先单测确认，若为空则取
  `accumulated.response_metadata["usage"]` 兜底）。
- 在 `_trace_thought(trace, step, reasoning_full)` 的条目上补 `tokens`：
  ```python
  trace.append({
      "type": "thought", "step": step, "chars": len(reasoning_full), "reasoning": reasoning_full,
      "tokens": _extract_usage(accumulated),   # {input, output, reasoning} | None
      "ms": round((time.perf_counter() - round_start) * 1000),
  })
  ```
- 新增模块级 helper `_extract_usage(msg) -> dict | None`：从 `usage_metadata` 归一化
  `{"input": int, "output": int, "reasoning": int}`，缺省返回 None（不破坏现有 trace 断言）。

### 3.2 tools_node（每个工具执行）
- `round_start` 记在工具循环前；每个 `tc` 执行 `_execute_tool` 前后计时。
- `tool_call` / `tool_result` 条目补 `ms`。

### 3.3 reflect_node（reflection 评估）
- `evaluate_draft` 目前只返回 `ReflectionVerdict`。**可选增强**：让它额外回传 usage
  （改返回 `(verdict, usage)` 或加 `reflection.py` 内部记录），reflection 条目补 `tokens`。
  **若嫌改签名，reflection 条目的 tokens 可暂缺**——不阻塞主线。

### 3.4 react_loop.py / turn_coordinator.py（run 汇总）
- graph 终态里已能拿到各 trace 条目的 `tokens`/`ms`；在
  `turn_coordinator._persist_agent_run`（ADR-0017 落库 helper）里做**聚合**：
  ```python
  usage = {
      "input_tokens": sum(...), "output_tokens": sum(...),
      "reasoning_tokens": sum(...), "total_ms": sum(...),
      "cost": _estimate_cost(input, output, reasoning),   # 估算，仅展示
      "model": settings.llm_model, "temperature": settings.llm_temperature,
  }
  ```

## 4. 存储（codeaware-py/app/ai/models/agent_run.py + alembic）

- **每步元数据**：直接进现有 `trace` JSONB（新增 `tokens`/`ms` key，无迁移，向前兼容——现有
  读取方忽略未知 key）。
- **run 汇总**：新增列（**alembic 0013**）：
  ```python
  # agent_runs 新增
  usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 见 §3.4 结构
  ```
  只加一列 JSONB（放汇总 dict），避免加 5 个标量列。序列化时给前端返回。
- **成本估算常量**（放 `app/core/config.py`，`@dataclass` 或 settings 字段）：
  DeepSeek 单价（元/百万 token）：输入 / 输出 / reasoning。**值在实施时按实际价格填**，默认给
  占位；价格变化只改 config 一处。

## 5. 前端展示（frontend/src）

### 5.1 流程视图（components/flowMap.ts + FlowTrace.tsx）
- `FlowNode.sub` 已有次要行；`buildFlowGraph` 里给每个节点按 trace 条目补
  `sub += f" · ⚡{tokens.total} tok · {ms}ms"`（取 entry.tokens/ms，无则不加）。
- `FlowTrace.tsx` 已渲染 `sub`，**大概率零改动**；验证即可。

### 5.2 时间线（pages/AgentRuns.tsx 的 Timeline）
- 每个 trace 条目行追加 token/ms 展示。

### 5.3 详情头部 / run 列表
- 详情页展示 run 汇总：`总 token` / `估算成本 ¥` / `模型`。
- run 列表列（可选）：总 token 或成本。**最低限度先只做详情页**，列表加列留给 UI 顺手时。

## 6. 配置

- 元数据捕获**默认开、不加开关**（纯元数据，符合哲学）。
- 成本估算若不想展示/计算，可加 `agent_trace_estimate_cost: bool = True` 开关（防价格写错时
  展示误导）。

## 7. 测试

- `tests/test_agent_ops.py`：
  - 现有 trace 断言 `[t["type"] for t in trace]` **不受影响**（新 key 是追加不是替换）。
  - **新增**：FakeAgentLLM 的 AIMessageChunk 加 `usage_metadata`，断言 thought 条目带
    `tokens`；run 落库断言 `usage` 汇总存在且数字正确。
- `tests/test_agent_graph.py`：断言流程 SSE 契约不变（token 元数据不改变事件）。
- 全量 `run_tests_safe.py` 357 passed 基线不破。

## 8. 验证

1. 定向：`run_tests_safe.py tests/test_agent_ops.py tests/test_agent_graph.py -q`
2. 全量回归 `run_tests_safe.py -q`
3. 真实体验：起项目（`./start.sh`）→ agent 模式问一个问题 → Agent Runs 详情页流程视图
   每个节点显示 `⚡tokens · ms`，详情头部显示总 token + 成本
4. 隐私确认：`agent_runs.trace` 里新增字段只有数字/模型名，**无对话内容**（与 reasoning 脱敏正交）

## 9. 实施顺序

1. `_extract_usage` helper + 单测确认 `accumulated.usage_metadata` 保留
2. agent_node / tools_node / reflect_node 补 tokens/ms（reflect 可选）
3. turn_coordinator 聚合 + agent_runs `usage` 列 + alembic 0013
4. 前端 flowMap / Timeline / 详情页展示
5. 测试更新 + 全量回归 + 提交（每步一提交，沿用打 tag 标准——此功能为 minor，完成后升 v1.5.0）

## 10. 边界

- **不接 LangSmith**：本计划是"用现有 trace 补深度"的替代方案
- **不存内容**：prompt/工具全量输出仍不做（B 方案），需要时另议开关
- **成本是估算**：DeepSeek 单价按 config 常量，非账单精确值，仅作展示参考
