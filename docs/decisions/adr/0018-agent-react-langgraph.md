# ADR-0018: ReAct 编排迁 LangGraph StateGraph + 轻量 Reflection

**状态**: 已实施（2026-08-14）
**日期**: 2026-08-14
**决策者**: Kevin

## 背景

Agent 模式（[ADR-0016](0016-react-agent-evaluation.md)）的主循环是手写 async generator
（`react_loop.py` 约 280 行），内含逐轮回注 reasoning_content、防打转 seen、per-tool 上限、
检索收敛检测、步数上限等一堆启发式。手写循环在单工具场景够用，但三个信号指向迁移：

1. **面试叙事**：LangGraph 是 Agent 编排主流标准，迁过去叙事完整（"循环控制迁进 StateGraph，
   对外 SSE 契约不变"）。
2. **可维护性**：团队接手读图比读手写循环快。
3. **扩展性**：ADR-0016 自己留的伏笔——"多工具复杂度上升时，LangGraph 编排可能更优"。
   并行工具流、条件子图、checkpoint、仓库感知等非线性演进，手写循环会吃力。

同时轻量加入 **Reflection**（生成后自评，不达标注入 feedback 再生成），验证效果。

## 决策

**1. 适配器方案**：保留 `react_loop` 的签名壳，内部换真 `StateGraph`（`agent_graph.py`），
对外契约（签名 / typed SSE 事件序列 / `ReactLoopState` 回填）零改动。`turn_coordinator.py`
零改动。

**2. 分两步**：第一步纯迁移（行为不变，回归跑绿）→ 第二步 Reflection（默认关）。

**3. Reflection 默认关**（`agent_reflection_enabled=False`，`agent_max_reflections=1`）。

### 图结构

```
START → agent → [有 tool_calls] → tools → [收敛 → 注入 HumanMessage + converged_pending] → agent
                ↘ [无 tool_calls，reflection 关] → END(final)
                ↘ [无 tool_calls，reflection 开] → reflect → [拒绝且未达上限 → 回 agent] / [接受/达上限 → END]
```

`AgentState`（TypedDict）承载原 `react_loop` 的局部状态：`messages`（就地累积）、`steps`、
`tool_counts`、`seen_calls`（json 化 key，序列化安全）、`observed_docs`、`round_doc_ids`、
`trace`、`stop_reason`、`tool_calls_total`、`error_tools`、`text`、`converged_pending`、`reflections`。

### 关键契约保真

- **收敛强制终答轮不递增 steps**：tools 收敛 → 注入 HumanMessage + `converged_pending` → 回
  agent；该轮 agent 不再 `steps+=1`，与原 `state.steps = round_no` 一致（`test_agent_loop` 断言
  steps==2、共 3 次 astream 通过）。
- **防打转 / per-tool 上限 / 收敛检测**逐条对照原实现，`tool_calls` 每次调用都 +1（含去重/超限），
  `error_tools` 只计真实异常。
- **sequence 现分配**：薄壳每个 yield 前调 `nxt()`，SSE `id==sequence` 严格递增。

## 为什么（含一处对计划 §4.2 的必要偏差）

计划原设想用 `astream_events(stream_mode=["custom","events"])`，经 `on_chat_model_stream`
转 token、`on_custom_event` 转工具事件。**实测 langgraph 1.2.10 不支持**：

1. `get_stream_writer()` 的 custom 事件不再经 `astream_events` 的 `on_custom_event` 出来，
   只能走 `graph.astream(stream_mode="custom")`。
2. 测试的 `FakeAgentLLM` 是普通类（非 runnable），`on_chat_model_stream` 不会触发。

**落地修正**：节点用 `get_stream_writer()` 主动发 custom 事件（`reasoning`/`token`/
`tool_call`/`tool_result`），薄壳用 `graph.astream(stream_mode=["custom","values"])` 转 SSE。
这与模型无关（真 `ChatDeepSeek` 与 `FakeAgentLLM` 同构），且让 Reflection 的 token 抑制
**天然成立**：reflect 节点走 `ainvoke`/结构化输出、不发 custom token，评估中间 token 不污染
前端回答流（无需按 `metadata.langgraph_node` 过滤）。

## 与其他 ADR 的关系

- [ADR-0014](0014-langchain-thin-adapter-no-langgraph.md)：其"手写 while 不引 LangGraph"结论
  已被本 ADR 覆盖——多工具复杂度上升时（ADR-0014/0016 预留的 re-evaluate 条件满足），工具
  循环迁 StateGraph。
- [ADR-0016](0016-react-agent-evaluation.md)：`react_loop.py` 从手写 while 变为薄壳，编排逻辑
  迁 `agent_graph.py`；停止判断（收敛/步数上限/防打转）语义不变。
- [ADR-0015](0015-langgraph-retrieval-enhancement.md)：检索层 LangGraph 路由不受影响，与本图的
  决策层是两回事。

## 不做

- **checkpoint 持久化**：`agent_runs` 已自落库（ADR-0017），图是单轮有状态编排，不引入
  LangGraph checkpoint。
- **`langgraph.prebuilt.create_react_agent`**：依赖未装的 langchain/community，且自定义节点
  更贴合现有启发式。
- **v3 实验 API**（beta）。
- **Reflection 结构化输出的生产化打磨**：默认关；反射模型默认复用 bind_tools 绑定的 model，
  thinking 绑定下 `with_structured_output` 可能退化到 ainvoke 回退，属已知限制，重启时再给
  反射单独的无 thinking 模型。
