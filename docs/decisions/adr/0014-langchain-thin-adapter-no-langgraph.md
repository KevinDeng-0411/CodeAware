# ADR-0014: LangChain 保持薄 adapter、不引入 LangGraph

**状态**: 已变更——原"不引入 LangGraph"决策已由 [ADR-0015](0015-langgraph-retrieval-enhancement.md)（检索增强）与 [ADR-0018](0018-agent-react-langgraph.md)（工具循环）先后覆盖；"完整 Agent / 多 Agent 编排不引入"结论不变。**变更细节见文末**
**日期**: 2026-08-05
**决策者**: Kevin

## 背景

探讨"从 LangChain 升级或结合 LangGraph 的可行性和预期效果"。项目定位约束：**Chat，最多贴近 Agent**（不走向完整 Agent）。

## 现状事实（探索确认）

| 维度 | 事实 |
|---|---|
| 版本 | langchain-core **1.5.1**（已是 1.x），langchain-deepseek 1.1.0 / ollama 1.1.0 / openai 1.4.1，**无 langchain 本体元包** |
| 耦合度 | 整个 `app/` 只有 [config.py](../) 一个文件 import LangChain |
| 领域交互 | 全部 duck-typing（`.content` / `additional_kwargs` / `ainvoke` / `astream` / `with_structured_output`），零消息类型依赖 |
| 结构化输出 | 4 处 `with_structured_output(json_mode)` + 手写 JSON 正则回退（DeepSeek thinking 不支持 json_schema/function_calling） |
| 测试 | 所有 FakeLLM 是普通 Python 类，零 LangChain 依赖 |

## 决策

**1. LangChain 不升级**：已是 1.x，且薄到可随时替换（换 provider SDK 只需改 config.py）。项目只用 `ainvoke`/`astream`/`with_structured_output` 三个原语，升级无业务收益。

**2. 不引入 LangGraph**：

| LangGraph 能力 | 判断 |
|---|---|
| 状态图 | Chat 状态机已手写（TurnCoordinator），迁移=重写验证等价，零收益 |
| checkpoint/断点 | 无长任务，不需要 |
| 工具循环 | 贴近 Agent 才需要——手写 20 行 while 循环即可 |
| 多 Agent 编排 | 无，不需要 |
| 节点 trace | 唯一实质收益，但 S3 前置（S2 分层）未做，迁移成本高 |

**3. S3 卡（确定性 Graph 双运行时）不启用**：S3 是为 S4 工具循环铺路。定位"最多贴近 Agent" → 未来即使做工具循环，手写循环（S4-lite 已设计）不需要 Graph 基础设施。S3 双运行时等价测试是平台化设计，个人项目规模下过度工程。

## 贴近 Agent 的最轻路径（触发时才做）

```text
Chat 状态机（当前）
  → 加 1 个工具（如 search_knowledge）
  → 手写 while 循环：LLM 决定调不调工具 → 调 → 结果回注 → 继续
  → 20 行，无 LangGraph
```

**触发条件**（满足任一）：真实需求"模型自主选工具"出现；面试需要展示工具循环 demo。

## 为什么（核心判断）

- LangChain 当前是**理想状态**：薄 adapter，领域逻辑不知其存在，替换成本≈改一个文件
- LangGraph 解决"复杂编排"问题——当前场景（单 Chat 状态机 + 最多 1 个工具）不复杂
- 与 reranker/意图识别/Context Recall 同一套逻辑：**看场景收益，不堆技术栈**

## 后续

- 若出现工具循环需求：手写 while 循环（S4-lite 卡），不引 LangGraph
- 若出现多 Agent/长任务/断点恢复需求：重新评估（当前无信号）

---

## 决策变更（2026-08-05）

用户明确要求引入 LangGraph 用于**检索层增强**（智能路由 + 自我纠错），见
[ADR-0015](0015-langgraph-retrieval-enhancement.md)。

**变更范围**：原"不引入 LangGraph"结论针对**完整 Agent 能力**（工具循环/checkpoint/多 Agent）——该结论不变。本次引入的是**检索层的模型决策形态**（贴近 Agent）：LangGraph StateGraph 表达智能路由 + 自我纠错，不执行工具、无 checkpoint。

**为什么不变更完整 Agent 结论**：ADR-0014 的核心判断（Chat 状态机手写足够、完整 Agent 超定位）仍然成立。LangGraph 本次只用于 RAG 检索决策，不改 Chat 状态机结构。

**LangChain 薄 adapter 结论不变**：仍只有 config.py import LangChain，领域逻辑 duck-typing 不受影响。

---

## 决策变更（2026-08-14）

Agent 工具循环（ReAct 主循环）已迁到 LangGraph StateGraph，见 [ADR-0018](0018-agent-react-langgraph.md)。
原"工具循环手写 20 行 while 即可 / 不引 LangGraph"的结论被覆盖——触发条件（多工具复杂度上升，
本 ADR 与 [ADR-0016](0016-react-agent-evaluation.md) 预留的"需 re-evaluate"）满足：Agent 有 5 个
工具 + 防打转 + per-tool 上限 + 收敛检测等启发式，手写循环的可维护性/扩展性收益不再成立。

**不变**："完整 Agent / 多 Agent 编排 / checkpoint 持久化"仍不引入——迁移的是**单轮有状态工具
循环的编排形态**（对外仍是薄 adapter：`react_loop` 签名与 SSE 契约不变），不是向多 Agent 平台化。

