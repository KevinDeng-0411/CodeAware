# LangGraph React 迁移 + Reflection 实施计划

> **状态**：已定稿待实施（2026-08-13）。实施顺序：物化 venv → 第一步纯迁移跑绿 → 第二步 Reflection → ADR 与文档 → 全量回归。
> **目标**：Agent 编排从手写 async generator 迁到 LangGraph StateGraph（面试/可维护性/扩展性动因），并轻量加入 Reflection 节点。
> **决策**：适配器（保留 `react_loop` 签名壳，内部换真 StateGraph，对外契约零改动）/ 分两步 / reflection 默认关。

---

## 1. 背景与动因

ReAct loop 当前是手写 async generator（`codeaware-py/app/ai/agent/react_loop.py`）。迁移动因：

1. **面试叙事**：LangGraph 是 Agent 编排主流标准，迁过去叙事更完整（"循环控制迁进 StateGraph，对外 SSE 契约不变"）。
2. **可维护性**：团队接手读图比读手写循环快；langgraph 生态/文档完善。
3. **扩展性**：手写循环瓶颈真实存在——一旦 Agent 非线性演进（并行工具流、条件子图、checkpoint、S4/S5 仓库感知），手写会吃力。这正是 ADR-0016 自己写的伏笔："多工具复杂度上升时，LangGraph 编排可能更优"。

同时加入 **Reflection 节点**（生成后自评，不达标注入 feedback 再生成），轻量实现验证效果。

## 2. 可行性结论（已实测，langgraph 1.2.10）

| 验证点 | 结论 |
|---|---|
| `astream_events(version="v2")` 逐 token 实时 | ✅ `on_chat_model_stream` 按 token 增量到达（慢模型 0.25s/token 间隔吻合），ReAct 多 superstep 无丢失/乱序 |
| 断连取消 | ✅ 外层 `aclose()` 干净取消（`finally: task.cancel()`），无 hang——SSE 客户端断开安全 |
| 节点形态 | ⚠️ 节点必须普通 async 函数（async generator 中间 yield 被丢弃）；工具/流程事件走 `stream_mode="custom"` + `get_stream_writer().emit()` → 外层 `on_custom_event` |
| 终态 | `on_chain_end`（name=LangGraph）的 `data["output"]` 即最终 AgentState（单次流，不重复跑图） |
| 环境阻断 | ⚠️ **当前 venv 原生扩展被云同步脱水**（psutil/onnxruntime/numpy `.so` blocks=0），`import langgraph` SIGBUS。**实施前必须 `uv sync` 物化** |

## 3. 环境前置（第一步前必做）

```bash
cd codeaware-py && uv sync
# 若 .so 仍脱水：uv pip install --reinstall psutil websockets numpy onnxruntime
```

## 4. 第一步：纯迁移（手写 while → StateGraph，行为不变）

### 4.1 `app/ai/agent/agent_graph.py`（新）——真 StateGraph

**AgentState**（TypedDict，承载原 react_loop 局部状态）：

```
messages: list          # LangChain messages（就地累积，AIMessage reasoning_content 回注 / ToolMessage）
steps: int
tool_counts: dict
seen_calls: list[str]   # 序列化安全：json 化 key，替代 set[tuple]
observed_docs: list[int]
round_doc_ids: list[int]
trace: list             # 与原 trace 条目格式完全一致（thought/tool_call/tool_result/answer/convergence_override）
stop_reason: str
tool_calls_total: int
error_tools: int
text: str
converged_pending: bool # 收敛已触发、等待强制终答轮
reflections: int        # 第二步用
```

**节点**（普通 async 函数，返回部分 state 更新）：
- `agent`：`model.astream(messages)` 聚合（AIMessageChunk 加法 + reasoning_content 提取）→ 回注 `AIMessage(content, tool_calls, additional_kwargs={reasoning_content})`；`_trace_thought`；无 tool_calls → `text`；`steps+=1`。收敛强制终答轮（converged_pending）若仍出 tool_calls → trace 标 `convergence_override`，忽略工具直接 text。
- `tools`：遍历 tool_calls——防打转（seen_calls）/ per-tool 上限（`TOOL_CALL_LIMITS`）/ `_execute_tool` 4 元组（is_exception → error_tools）；`get_stream_writer().emit({type:"tool_call"/"tool_result", ...})` 发 custom 事件；更新 trace / round_doc_ids / messages（ToolMessage 截断）。

**条件边**：
- `agent →` 有 tool_calls ? `tools` : 无工具（终答）→ 收敛检查 → END 或 reflection（第二步）
- `tools →` `round_doc_ids ⊆ observed_docs`（收敛，对照现有 `if round_doc_ids and round_doc_ids <= observed_docs`）→ 注入 `HumanMessage("已获得足够信息…")` + `converged_pending=True` → 回 `agent`（**FakeAgentLLM 时序契约：一轮两次 astream，与手写一致**）；`steps >= max_steps` → END(stop=max_steps)；否则回 `agent`

### 4.2 `app/ai/agent/react_loop.py`（重构）——薄壳：签名不变

```python
async def react_loop(model, messages, tool_map, cid, turn_id, nxt, state, max_steps=4):
    graph = build_agent_graph(model, tool_map, max_steps)
    async for ev in graph.astream_events(init, version="v2", stream_mode=["custom", "events"]):
        if ev["event"] == "on_chat_model_stream":
            # reasoning → ReasoningDelta；content → TokenDelta（sequence=nxt()）
        elif ev["event"] == "on_custom_event":
            # payload type == "tool_call"/"tool_result" → ToolCall/ToolResult（sequence=nxt()）
        elif ev["event"] == "on_chain_end" and ev["name"] == "LangGraph":
            final = ev["data"]["output"]
    # 终态回填 state（text/steps/trace/stop_reason/tool_calls/error_tools）
```

**要点**：
- **sequence**：每个 yield 前调共享 `nxt()` 现分配（SSE `id==sequence` 严格递增 + coordinator 其它事件连续）
- **事件格式**：ToolCall/ToolResult 用原 schema（tool_call_id 配对）；trace 条目格式不动
- **取消**：generator 外层 `aclose` 取消 astream_events（已验证），coordinator `_ClosingStreamingResponse` 传播不变

### 4.3 `turn_coordinator.py`——零改动（验证 L407-416 透传 + 三终端落库 + 共享 nxt 不动）

## 5. 第二步：Reflection 节点（轻量，默认关）

- `app/ai/agent/reflection.py`（新）：`ReflectionVerdict(BaseModel)`（`accepted: bool, feedback: str`）；`reflect` 节点用 `model.with_structured_output(ReflectionVerdict, method="json_mode")`（回退 ainvoke）评估 draft
- 条件边（agent 无工具产出 draft 后）：`enabled and not accepted and reflections < max_reflections` → 注入 feedback → 回 `agent` 再生成；否则 END(stop=final)
- AgentState 加 `reflections: int`
- **token 抑制（关键）**：reflect 节点模型调用也会发 `on_chat_model_stream`，会污染前端回答流——外层转译层用事件 `metadata["langgraph_node"]` 区分节点：**agent 节点 token → yield 前端；reflect 节点 token → 丢弃**
- config：`agent_reflection_enabled: bool = False`、`agent_max_reflections: int = 1`（+ `.env.example`）

## 6. 测试

- **第一步回归**（断言应保持，FakeAgentLLM 时序契约核对）：
  - `tests/test_agent_loop.py`（8）：收敛路径"一轮两次 astream"在图里复现
  - `tests/test_agent_ops.py`：trace 类型序列 / stop_reason / error_tools 断言不变
  - `tests/eval/test_agent_eval.py`（live_eval）：重跑门禁（recall ≥0.7 / closure ≥0.9 / direct 100%）
  - **新增** `tests/test_agent_graph.py`：turn_coordinator agent 分支 SSE 流式测试（当前缺失）——mock LLM 下发 tool.call/tool.result 顺序 + sequence 单调 + 唯一终态
- **第二步** `tests/test_reflection.py`：enabled 时 accepted / rejected / 达上限；token 抑制验证
- 全量 `run_tests_safe.py`；前端不受影响（协议冻结，前端 `sseParser.ts` fail-closed 校验不变）

## 7. 文档

- `docs/decisions/adr/0018-agent-react-langgraph.md`：ReAct 编排迁 StateGraph（适配器保留对外契约）+ Reflection；修订 ADR-0014/0016"手写 while"表述为已迁
- 面试文档 6.19/6.20 更新（编排决策叙事）；CLAUDE.md 当前状态

## 8. 实施顺序

1. `uv sync` 物化 venv（前置）
2. 第一步：agent_graph.py + react_loop 壳 + 测试回归 + live_eval
3. 第二步：reflection.py + config + 测试
4. ADR-0018 + 文档 + 全量回归 + 提交

## 9. 风险与边界

- **不做**：checkpoint 持久化（AgentRun 已自落库，图是单轮有状态编排）；`langgraph.prebuilt.create_react_agent`（依赖未装的 langchain/community，且自定义节点更贴合现有启发式）；v3 实验 API（beta）
- **风险**：
  - FakeAgentLLM 调用时序错位（收敛 / 防打转的图路径必须逐条对照现有实现）
  - reflect token 污染回答流（必须 `metadata.langgraph_node` 抑制）
  - venv 物化失败（环境：若 iCloud 持续脱水，改用系统级 venv 重建）
  - sequence 必须 yield 时现分配（预生成/分批会破坏单调性，前端 `SEQUENCE_MISMATCH` fail-closed）
