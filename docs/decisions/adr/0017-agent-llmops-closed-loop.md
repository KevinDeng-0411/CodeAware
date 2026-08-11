# ADR-0017: Agent LLMOps 闭环——run trace + 失败沉淀 + 请求边界 guardrail + memory 观测

**状态**: 已实施（2026-08-11）
**日期**: 2026-08-11
**决策者**: Kevin

## 背景

Agent 模式（[ADR-0016](0016-react-agent-evaluation.md)）落地后，三个问题仍未解决：

1. **Agent 是黑盒**：react_loop 的 SSE 事件（reasoning/tool.call/tool.result）播完即弃
   （`chat_events.py` 明确"展示过程，不持久化"），`messages` 表只存头尾。线上 run 跑完
   没有任何结构化轨迹可回放——"为什么 agent 反复调 get_document"答不上来。
2. **失败不沉淀**：eval case 集（`tests/eval/test_agent_eval.py` 的 AGENT_CASES）手动维护，
   线上失败不会自动变成回归样本，"可持续改进"靠人勤奋而非系统。
3. **无 guardrail 层**：Agent 从用户查询取指令，无注入防护。

目标：把"黑盒、失败不沉淀"变成"可观测、可评测、可持续改进"的闭环，且在前端可见可交互。

## 决策

### 1. Run Trace 持久化 + 回放（观测）

- 新表 `agent_runs`（turn_id 全局唯一 = SSE turn_id；FK conversations CASCADE）：
  `trace` JSONB（thought/tool_call/tool_result/answer 按序 + convergence_override 防御标记）、
  `context_snapshot` JSONB（本轮上下文：summary + window 边界 + memory_refs）、
  status（completed|empty|error|cancelled）/ stop_reason（final|no_output|max_steps|converged|error|cancelled）、
  steps/tool_calls/error_tools、失败沉淀字段（needs_review/review_status/expected_tools/category/synced）。
- react_loop 在 `ReactLoopState` 累积 trace（纯追加字段，向后兼容 eval）；`_execute_tool`
  返回扩为 4 元组区分"工具真实异常"（计入 error_tools）与"正常返回错误结果"。
- `turn_coordinator.run()` 三终端点（成功/模型异常/CancelledError）经单 helper
  `_persist_agent_run` best-effort 落库（失败只 warning 不 fail turn）；CancelledError 也落
  cancelled run（客户端断开是有价值的失败模式）。
- 回放端点：`GET /agent-runs`（分页+过滤）、`GET /agent-runs/{turn_id}`、`GET /agent-runs/stats`、
  `POST /agent-runs/{turn_id}/review`。归属校验 JOIN conversations（null 会话对登录用户可见）。
- **trace 默认只存元数据**：thought 条目存 `{step, chars}`，完整 reasoning 由
  `agent_trace_include_reasoning`（默认 False）开关控制——与"思考是过程不是内容"哲学一致，
  避免 JSONB 膨胀与隐私（reasoning 会反射用户查询）。
- `context_snapshot` 只存消息窗口边界（count）+ 摘要 + memory_refs，不逐条复制消息全文
  （全文在 messages 表）。快照语义 = "这轮实际注入了什么"，与对话跳转（完整上下文）互补。

### 2. 失败沉淀 → eval 回归集（可持续改进）

- `needs_review` 分层：`status==error` 或 `status==empty` 或 `error_tools>0` → 待评审；
  cancelled 除外。与 eval 的 closure 语义一致（空终答 = 失败）。
- **API review 流**：`POST /agent-runs/{turn_id}/review` 落 `review_status`
  （accepted 需 expected_tools + category；rejected 直接拒绝）。
- `scripts/sync_regression_cases.py`：accepted 且未 synced 的 run → 追加进
  `tests/eval/regression_cases.py`（REGRESSION_CASES），`test_agent_eval.py` 的
  AGENT_CASES 自动拼接（保留人工维护的 BASE 18 个）。幂等（按 query/tools/category 去重）。
- 比离线 collect/apply 脚本更干净：review 状态在 DB、与回放列表天然组合、无文件名/水印管理。

### 3. Guardrail：请求边界（fail-closed）

- 注入检测在 **`ChatRequest.message` Pydantic validator**（请求边界，fail-closed 拒绝，
  HTTP 422），RAG/Agent 双模式生效。`guardrails_enabled`（默认 True）开关。
- **刻意不在工具结果层做注入检测**：知识库是策展内容，对检索/文档内容做模式匹配会误报
  （SQL/XSS 教学文档满屏关键字）且破坏模型理解。真实注入向量是用户查询覆盖系统指令，
  故拦截点在请求边界（[`app/ai/agent/guardrails.py`](../../../codeaware-py/app/ai/agent/guardrails.py) 纯函数）。
- 模式保守：要求"覆盖指令 + 系统上下文"同时出现，宁可漏放不误伤正常提问。

### 4. Memory-Ops 两个 counter（观测）

- `metrics.memory` Kafka 事件（`MemoryMetricsEvent`）：
  - **recall**：`context_builder.build()`（RAG）与 `build_agent_messages()`（Agent）的
    记忆命中块各发一次（count = 注入记忆数）——两条召回路径都覆盖。
  - **extraction**：`memory_extract.py` 各 return 点发（含 count=0 的早退原因）——抽取频率可观测。
- fire-and-forget，无 producer 时静默，绝不影响调用方。

### 5. 前端 Agent Runs 页（可见可交互）

- 新页面（SPA `PageId` 切换）：统计条 + 列表（status/needs_review 筛选 + 分页）+ 详情回放抽屉。
- **流程视图 = 三层架构**（参照主界面 trace 机制，事件源无关）：
  - ① 静态 SVG 层：每 run 生成一次节点/边骨架，稳定 node id；
  - ② 事件流层：归一化 trace 条目按序喂入（SSE 推实时 + 轮询补全局）；
  - ③ STAGE 映射表（`flowMap.ts`）：条目 → node id，点亮 = CSS class 切换（非逐帧 JS 动画）。
  - 事件源无关使 P2（Chat 页实时高亮）变为换数据源而非重做。
- **跳转**：doc 节点 → Knowledge 页打开该文档（`tool_result` 带 doc_ids）；
  "查看对话" → Chat 页完整上下文；记忆区 → Memory 页。统一走 `store/agentOps.ts`（zustand）。
- **评审**：详情内 accept（expected_tools 多选 + category）/reject → 落 review_status。

## 权衡

- **trace 存元数据而非 reasoning 全文**：默认放弃完整思考回放（调试价值）换存储/隐私安全；
  需要时可 `AGENT_TRACE_INCLUDE_REASONING=true` 打开。
- **guardrail 在请求边界**：放弃了"文档内容不可信"的防护（知识库策展内容，威胁模型低），
  换低误报 + 不伤模型理解。文档内容注入若未来需要，应在上传摄取时做，不在检索时做。
- **自建可观测而非引入 Langfuse/LangSmith**：与 [ADR-0014](0014-langchain-thin-adapter-no-langgraph.md)
  薄 adapter 哲学一致；面试叙事上"自己写一层薄的 trace+replay"比"接了个 SaaS"深。
- **不引 mermaid npm 库**：流程视图自建轻 SVG 组件（三层架构），零重依赖、vitest 可测；
  mermaid 是静态渲染库，步进高亮要事后改 SVG class，脆。

## 与既有约束的关系

- 不动 typed SSE 协议（protocol_version=1 冻结）：trace 是独立存储，与流式展示解耦。
- 不改 `_txn_assistant` 空文本持久化行为（现有行为，出 scope）。
- PG 真相源：agent_runs 是 observability 记录，短事务 best-effort 写；失败不破坏 turn。
- 全异步短事务：`_persist_agent_run` 自建 AsyncSessionLocal，不跨模型等待持有连接。

## 实施产物

- 后端：`app/models/agent_run.py` + 迁移 0012、`react_loop.py` trace 累积、
  `turn_coordinator.py` 持久化、`app/api/v1/chat.py` 4 端点、`app/schemas/agent_run.py`、
  `app/ai/agent/guardrails.py`、`events` MemoryMetricsEvent、`context_builder`/`memory_extract` 埋点、
  `scripts/sync_regression_cases.py`、`tests/eval/regression_cases.py`。
- 前端：`pages/AgentRuns.tsx`、`components/FlowTrace.tsx` + `flowMap.ts`、
  `components/ToolTrace.tsx`（抽取共享）、`store/agentOps.ts`、Chat/Knowledge 页 focus 消费。
- 测试：`tests/test_agent_ops.py`（15 个）、`flowMap.test.ts` + `client.test.ts` agentRuns 块。
- 全量后端 350 passed（原 335 + 新 15）；前端 53 passed + lint + tsc 干净。

## 不做

- 不做 Chat 页实时高亮（P2）：架构已预留（事件源无关），未来换 SSE 数据源即可。
- 不做代码文件跳转（节点跳转 = 知识库内容，用户澄清）。
- 不引 mermaid 库、不集成 Langfuse/LangSmith。
- Memory 两 counter 为后端 Kafka 事件（消费落 ops 日志），页面统计条暂不展示（需计数存储，后续）。
- 不改 SSE 协议、不改空 assistant 持久化行为。
