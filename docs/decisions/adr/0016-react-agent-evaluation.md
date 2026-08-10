# ADR-0016: ReAct Agent 升级评估--thinking 模式 tool calling 原型验证

**状态**: 已实施（2026-08-10 追加：agent 模式落地，CHAT_MODE=rag|agent）
**日期**: 2026-08-10
**决策者**: Kevin

## 背景

用户希望将当前 Chat（确定性状态机 + [ADR-0015](0015-langgraph-retrieval-enhancement.md) LangGraph 检索增强）升级为真正的 ReAct Agent--对话中加入 tool calling 机制，模型自主选取工具回答。

评估识别出**最关键的未知**：thinking 模式下，LangChain `ChatDeepSeek.bind_tools` + `astream` 多轮循环时，能否把含 `reasoning_content` 的 assistant message 回传给下一轮而不 400。[deepseek-notes.md](../../integration/deepseek-notes.md) §2 已记录 thinking 模式工具调用的三条硬约束，但示例用裸 OpenAI client；项目生产化要用 LangChain 路径（保持 adapter 一致性），该路径**未验证**--当前代码只读 reasoning_content 做展示、从不回注。

本 ADR 记录原型验证结论与"当前是否实施完整 ReAct"的决策。

## 约束识别（完整 ReAct 落地需正视的 5 点）

| # | 约束 | 现状 |
|---|---|---|
| 1 | **thinking 模式 reasoning_content 逐轮回传** | 原型验证（见下） |
| 2 | **typed SSE 协议扩展** | 8 事件冻结（protocol_version=1），缺 `tool.call`/`tool.result`；前端 `sseParser.ts` 对未知事件抛 `UNKNOWN_EVENT`，需前后端同步升级 |
| 3 | **与 ADR-0015 的关系** | 当前 LangGraph 路由（retrieve/direct）本质是"模型决定要不要检索"；ReAct 下检索变成 tool，会吸收/取代 ADR-0015 路由 + 自我纠错（60/60 成绩有回归风险） |
| 4 | **事务边界** | TurnCoordinator 的短事务（ADR-0003）是核心设计；ReAct 主循环若塞进 LangGraph 节点会模糊事务边界 |
| 5 | **成本与首 token 张力** | ReAct 每步 = 1 次 LLM 调用 + 工具执行，延迟与 token 上升，与 typed SSE"首 token 快"卖点冲突 |

## 原型验证

脚本：[`codeaware-py/scripts/verify_react_thinking.py`](../../../codeaware-py/scripts/verify_react_thinking.py)
路径 A（LangChain `bind_tools` + `astream` + `AIMessage(additional_kwargs={"reasoning_content": ...})` 回传），3 个用例（时间 / 计算 / 多步并行），真实 DeepSeek v4-flash thinking 模式。

**三个假设全部证实：**

| 假设 | 结论 | 证据 |
|---|---|---|
| H1 thinking 多轮不 400 + reasoning_content 回传 | ✅ | 第 2 轮模型基于工具结果生成终答，证明含 reasoning_content 的 AIMessage 回传成功，无 400 |
| H2 流式 tool_calls 聚合 | ✅ | `AIMessageChunk +` 自动合并分片；用例 3 **并行调 2 个工具**（get_current_time + calculate），聚合无误 |
| H3 工具闭环（选工具->执行->终答） | ✅ | 模型正确选用工具、基于 ToolMessage 观察结果生成最终答案 |

**关键发现**：reasoning_content 回传比预期简单--`AIMessage(content=..., tool_calls=..., additional_kwargs={"reasoning_content": reasoning})` 直接工作，无需手工拼装 OpenAI message 格式。thinking 模式 + `bind_tools(tool_choice="auto", extra_body={"thinking":{"type":"enabled"}})` 即可。

## 实施结果（2026-08-10）

用户明确要求实施 agent 模式线，重启条件 1 触发（真实的多步推理场景需求）。**决策从"暂缓"转为"双模式落地"**：RAG 模式保持默认且零改动，Agent 模式作为 CHAT_MODE=agent 分支新增。

| 实施项 | 内容 |
|---|---|
| 开关 | `config.py` + `chat_mode`（rag\|agent，默认 rag，.env.example + CHAT_MODE） |
| 工具集 | `app/ai/agent/tools.py`：search_knowledge（套壳 RagService 保 MRR 0.941 流水线）/ get_document（全文截断）/ list_documents / calculate / get_current_time |
| ReAct 循环 | `app/ai/agent/react_loop.py`：thinking reasoning_content 逐轮回注、流式 tool_calls 聚合、防打转去重、步数上限、工具失败降级 |
| 分流 | `turn_coordinator.py` run() 按 chat_mode 分流；agent 走 build_agent_messages（跳过 RAG 预检索，检索决策交给工具）+ react_loop，rag 走原单次 astream（零改动） |
| SSE | chat_events.py + ToolCall/ToolResult 事件（protocol_version 保持 1）；前端 chatEvents.ts/sseParser.ts/Chat.tsx 同步，ToolTrace 折叠面板渲染工具轨迹 |
| 验证 | 后端 329 passed（+14 agent 测试）；前端 43 passed + lint + build；live smoke 真实 DeepSeek：calculate / get_current_time / search_knowledge+get_document 链式调用全部工作 |

**与暂缓决策的关系**：暂缓的是"用完整 ReAct 替代 RAG"（动已验证设计、ROI 低）；实际落地的是"双模式并存"（RAG 处理 90% 单轮问题，Agent 预留 10% 多步推理），与 [ADR-0009](0009-reranker-deferred.md) "评估后暂缓、条件满足后落地"同模式。

## 决策：当前不实施完整 ReAct

**评估后暂缓**，呼应 [ADR-0009](0009-reranker-deferred.md) reranker 的"评估->暂缓->依赖条件满足后落地"模式。理由：

1. **无真实多步推理场景需求**。用户 90%+ 是知识问题，单轮 RAG（路由 60/60、MRR 0.941）已解决；当前没有"模型必须自主选工具"的真实业务场景（与 ADR-0014 当初判断一致）。
2. **动已验证设计 ROI 低**。完整 ReAct 要触碰 SSE 协议（冻结的 protocol_version=1）、ADR-0015 的 60/60 路由、事务外壳（ADR-0003 核心）--高风险动已验证的东西，换不确定收益。
3. **成本与"首 token 快"冲突**。多步循环拉长首响应，与当前卖点张力大。

## 生产化路径（已验证可行，留待重启）

原型已证明技术底座可行。若未来重启完整 ReAct，路径明确：

### 双模式演进框架：CHAT_MODE=rag|agent

ReAct 优势在"多步推理"，劣势在"对简单问题太重"（延迟/成本/可靠性）。故不替代 RAG，而是**双模式各司其职**，复用 ADR-0015 的 `RAG_RUNTIME` 双运行时先例：

| 模式 | 适用场景 | 流程 |
|---|---|---|
| `rag`（默认，当前） | 90% 单轮知识问题 | ADR-0015 检索决策图 + 单次生成（快、省、稳，MRR 0.941） |
| `agent`（重启后） | 10% 多步推理问题 | ReAct 工具循环：模型自主选工具、条件依赖多步检索、观察后再决策 |

**触发 agent 模式的判定**待定（手动切换 / 系统判断）。系统自动判断 = 意图识别，ADR 评估后不做；初期建议手动切换或复杂度启发式。

### Agent 模式工具集

工具设计原则：**不重复 ADR-0015 已做的事**（否则只是换壳跑同流程，增量价值为零）。工具应是当前 RAG 做不到的能力。

| 工具 | 来源 | 价值 |
|---|---|---|
| `search_knowledge(query)` | 套壳 `RagService.search_prepared` | 基础；价值在模型自主决定何时查/查够没 |
| `get_document(doc_id\|title)` | 新建（按 id 现成、按 title 需补） | **核心增量**：chunk 检索只给片段，文档级浏览是 RAG 做不到的 |
| `list_documents(filter?)` | 套壳现有 API | 配合 get_document |
| `calculate(expression)` | 原型已实现 | 解决 direct 路由的"1+1"尴尬 |
| `get_current_time()` | 原型已实现 | 解决"今天几号" |

不做：记忆召回工具化（便宜+安全，无条件注入更稳）、代码搜索（chat-to-agent S5 范围）。

### ReAct 优势/劣势权衡

优势：① 条件依赖多步检索（先查A再据A查B，单轮 RAG 结构上做不到）；② 检索-生成动态交织（生成中发现缺口能补查）；③ 工具按需组合；④ thought-action-observation 链可解释。
劣势：① 多轮 = 多次 LLM 调用，首响应慢（与"首 token 快"冲突）；② token 成本数倍；③ 模型选错工具/死循环风险（需步数上限 + 复用 rag_graph seen_queries 防打转）；④ 90% 单轮问题用 ReAct 是杀鸡用牛刀。

### 实现要点

- **主循环宿主**：`TurnCoordinator.run()` 的 model stream 块（单次 `astream` -> 工具循环）。手写 while 或 LangGraph StateGraph 均可（ADR-0014"手写 while"前提在多工具下不再成立，需 re-evaluate）。
- **模型调用**：`ChatDeepSeek.bind_tools(tools, tool_choice="auto", extra_body={"thinking":{"type":"enabled"}})` + `astream`，每轮 `AIMessage(additional_kwargs={"reasoning_content": ...})` 回注（原型已验证）。
- **SSE 协议**：新增 `tool.call` / `tool.result` 事件，protocol_version 升级，前后端 `chat_events.py`/`chatEvents.ts`/`sseParser.ts` 同步。thought 走 `reasoning.delta`（契合"思考不落库"哲学）。
- **ADR-0015 关系**：分阶段--先"前置 RAG 兜底 + ReAct 工具增强"（ADR-0015 不动），再"检索变 tool、ADR-0015 路由退役"（需重跑 golden 验证）。
- **降级**：工具失败/步数上限回退单次生成；复用 rag_graph 的 seen_queries 防打转模式。

### MCP 远期选项（当前不引入）

工具暴露方式当前用 `@tool`（最简单）。MCP（Model Context Protocol）是远期选项--当 CodeAware 要接入外部工具生态（消费）或被外部 Agent 消费（Claude Desktop/Cursor 查 CodeAware 知识库）时引入。DeepSeek 用 MCP 需经 `langchain-mcp-adapters` 适配（底层仍走 bind_tools）。

**关键架构卫生**：工具业务逻辑（`RagService` 等）已与暴露方式解耦--`@tool` 只是套壳，未来切 MCP 只需套 server 壳，逻辑层不改。当前不引入、保持解耦即可（不为假想需求付协议税，但留低成本切换路径）。

## 重启条件（满足任一）

1. 出现真实的多步推理场景需求（如"先查文档 A 再查文档 B 再综合"频繁出现）
2. 工具循环成为检索质量或回答能力的瓶颈
3. SSE 协议升级窗口（与其他协议演进合并）

## 与其他 ADR 的关系

- [ADR-0014](0014-langchain-thin-adapter-no-langgraph.md)：原"不引入 LangGraph"针对完整 Agent，本 ADR 维持"当前不做完整 Agent"结论；若重启，ADR-0014"手写 while"前提需 re-evaluate（多工具复杂度上升，LangGraph 编排可能更优）。
- [ADR-0015](0015-langgraph-retrieval-enhancement.md)：检索层 LangGraph 路由保持不变；完整 ReAct 落地时其路由/纠错可被工具选择吸收，届时需 re-evaluate。
- [ADR-0009](0009-reranker-deferred.md)：同为"评估后暂缓、留重启条件"模式。

## 不做

- 不实施完整 ReAct（不动 TurnCoordinator / ContextBuilder / RagGraph / SSE 协议 / 前端）
- 不扩展 typed SSE 协议
- 不做知识库检索工具化（需 DB，属生产化阶段）
- 不引入 LangGraph 编排 ReAct 循环（原型用手写 while 验证底层假设；LangGraph vs while 是生产化决策）
