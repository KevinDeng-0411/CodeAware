# 文档索引（INDEX）

> **编码前先查此索引**：按功能/编码场景定位相关文档与章节，再按指引阅读。
> 文档按关注点分目录：`roadmap/`（渐进升级执行卡）、`migration/`（迁移蓝图）、`decisions/`（ADR + 术语表）、`integration/`（外部集成）、`interview/`（面试）。

## 如何用

1. 要编码某功能 -> 在下表查「功能」行 -> 读对应 ADR 与当前执行卡。
2. ADR 负责长期语义；`current-release/` 负责 C1–C3 当前版本与 C4 BM25 增强实施；
   `migration/` 只作迁移历史与背景。
3. 当前先按[升级总入口](roadmap/README.md)完成 C1–C4；只有
   [机器可校验证据](roadmap/证据清单与解锁规则.md)可以改变阶段状态。
4. Chat → Agent 是锁定的未来方向；个人项目默认按
   [`personal-local-readonly`](roadmap/chat-to-agent/personal/README.md) 的
   `S1-lite → S2-lite → S4-lite → S5-lite` 实施，C4 后仍需逐卡另行授权。
5. 所有 coding agent 共用的编码铁律、技术栈、目录结构见根目录 `AGENTS.md`。

## 功能 -> 文档映射

| 功能 / 编码场景 | ADR | 迁移文档章节 | 其他 |
|---|---|---|---|
| 总览 / 核心域=Chat | [ADR-0007](decisions/adr/0007-core-domain-and-bounded-contexts.md) | §1 §9 | [术语表](decisions/glossary.md) |
| 迁移路线图 / 阶段验收 | - | [§6 路线图](migration/Python重构迁移文档.md) · §11 清单 | - |
| 后续升级 / 缺口与预留 | - | [后续升级计划](migration/后续升级计划.md) | - |
| 当前版本与检索增强 | [ADR-0001~0007](decisions/adr/) | - | [C1–C4](roadmap/current-release/README.md) · [C4 BM25](roadmap/current-release/04-BM25检索增强.md) · [证据模板](roadmap/current-release/验收证据模板.md) |
| 阶段解锁 / 机器可校验证据 | - | - | [证据清单与解锁规则](roadmap/证据清单与解锁规则.md) |
| Chat → Agent 个人默认路线（未来、锁定） | [ADR-0007](decisions/adr/0007-core-domain-and-bounded-contexts.md) | - | [个人路线](roadmap/chat-to-agent/personal/README.md) · [总入口](roadmap/chat-to-agent/README.md) · [公共契约](roadmap/chat-to-agent/00-执行约定与公共契约.md) |
| 技术选型 / AI 搜索 / Agent 能力地图 | [ADR-0007](decisions/adr/0007-core-domain-and-bounded-contexts.md) | - | [技术选型与能力地图](roadmap/技术选型与能力地图.md) |
| Agent Tool / Citation / SSE 事件 | - | - | [精简 S4](roadmap/chat-to-agent/personal/S4-只读工具Agent.md) · [公共契约](roadmap/chat-to-agent/00-执行约定与公共契约.md) |
| Agent Run / Artifact / Approval（条件型） | - | - | [可选升级触发条件](roadmap/chat-to-agent/personal/可选升级触发条件.md) · [平台参考契约](roadmap/chat-to-agent/00-执行约定与公共契约.md) |
| 阶段闭环 / 演示 / 验收证据 | - | - | [统一规则](roadmap/证据清单与解锁规则.md) · [当前模板](roadmap/current-release/验收证据模板.md) · [Agent 模板](roadmap/chat-to-agent/验收证据模板.md) |
| 数据模型（9 表） | 0001 / 0002 / 0004 / 0005 / 0006 | §7.2.2 | - |
| 向量召回基建 VectorRecallService | [0001](decisions/adr/0001-memory-vs-knowledge-two-tables-shared-recall.md) | §7.3 | - |
| 短期记忆（滑窗+摘要+PG fallback） | [0003](decisions/adr/0003-message-store-pg-source-of-truth.md) · [0004](decisions/adr/0004-memory-concept-and-conversation-naming.md) | §7.6 | - |
| 长期记忆（内联向量召回） | [0001](decisions/adr/0001-memory-vs-knowledge-two-tables-shared-recall.md) · [0004](decisions/adr/0004-memory-concept-and-conversation-naming.md) | §7.7 | - |
| Knowledge / RAG（父子表+混合检索） | [0001](decisions/adr/0001-memory-vs-knowledge-two-tables-shared-recall.md) · [0002](decisions/adr/0002-knowledge-document-parent-child.md) | §7.2.2 · §7.5 | - |
| Conversation / Chat（SSE+CHAT 模板） | [0004](decisions/adr/0004-memory-concept-and-conversation-naming.md) | §7.8 | - |
| Code Review（结构化输出） | [0005](decisions/adr/0005-prompttemplate-versioning-activation-chat.md) · [0006](decisions/adr/0006-records-audit-log-merge.md) | §7.4 · §7.11 | - |
| Prompt 模板（版本化+激活） | [0005](decisions/adr/0005-prompttemplate-versioning-activation-chat.md) | §7.11 | - |
| Records（审计日志合并） | [0006](decisions/adr/0006-records-audit-log-merge.md) | §7.2.2 | - |
| 单测生成 / AIReadMe | - | §7.4（流程同 CR） | - |
| DeepSeek / LLM 集成 | - | - | [deepseek-notes](integration/deepseek-notes.md) |
| 测试策略 / 覆盖率方针 | - | §6.2 · §6.3 | `AGENTS.md` 测试规则 · [testing-notes](migration/testing-notes.md) |
| 面试话术 | - | §9 | [面试准备指南](interview/面试准备指南.md) |

## 文档清单

| 路径 | 内容 |
|------|------|
| [roadmap/README.md](roadmap/README.md) | 升级总入口：先当前版本，后未来 Agent；含硬门禁和文档权威边界 |
| [roadmap/技术选型与能力地图.md](roadmap/技术选型与能力地图.md) | 当前保留/新增技术、搜索/RAG 与 Agent 能力差距、未来选型触发条件 |
| [roadmap/模型实施任务模板.md](roadmap/模型实施任务模板.md) | 可直接交给其他编码模型的单阶段实施/只读评审任务模板 |
| [roadmap/证据清单与解锁规则.md](roadmap/证据清单与解锁规则.md) | manifest、产物哈希、安全测试、回退边界与逐阶段授权规则 |
| [roadmap/current-release/README.md](roadmap/current-release/README.md) | C1 缺口修复、C2 七域闭环、C3 版本冻结与 C4 BM25 检索增强 |
| [roadmap/current-release/01-当前缺口修复.md](roadmap/current-release/01-当前缺口修复.md) | 修复 typed SSE、摘要、multipart、空环境和真实 AIReadMe |
| [roadmap/current-release/02-现有功能闭环验收.md](roadmap/current-release/02-现有功能闭环验收.md) | 现有 7 个功能域的契约、测试、持久化和 UI 演示闭环 |
| [roadmap/current-release/03-版本冻结与交接.md](roadmap/current-release/03-版本冻结与交接.md) | 文档/OpenAPI/配置校准、空环境复现、指标与 Agent 解锁条件 |
| [roadmap/current-release/C3-交接运行手册.md](roadmap/current-release/C3-交接运行手册.md) | 新接手者从干净仓库执行冻结验证、聚焦演示和安全回退 |
| [roadmap/current-release/04-BM25检索增强.md](roadmap/current-release/04-BM25检索增强.md) | C3 后以真实 BM25 替换 pg_trgm 词法腿，完成评测、融合、回退与 Evidence |
| [roadmap/current-release/验收证据模板.md](roadmap/current-release/验收证据模板.md) | 当前版本每阶段必须提交的验收证据 |
| [releases/0.1.0.md](releases/0.1.0.md) | 当前冻结版本的能力、契约变化、迁移、限制和回退说明 |
| [roadmap/chat-to-agent/README.md](roadmap/chat-to-agent/README.md) | 个人默认路线总入口、能力 DAG、门禁与平台参考边界 |
| [roadmap/chat-to-agent/personal/README.md](roadmap/chat-to-agent/personal/README.md) | `personal-local-readonly` 默认档案：C1–C4 → S1-lite → S2-lite → S4-lite → S5-lite |
| [roadmap/chat-to-agent/personal/S1-精简项目隔离.md](roadmap/chat-to-agent/personal/S1-精简项目隔离.md) | S1-lite：最小 Project 表、五个父实体作用域与跨项目隔离 |
| [roadmap/chat-to-agent/personal/S2-轻量分层.md](roadmap/chat-to-agent/personal/S2-轻量分层.md) | S2-lite：ReplyEngine、Context、read ports 与短事务边界 |
| [roadmap/chat-to-agent/personal/S4-只读工具Agent.md](roadmap/chat-to-agent/personal/S4-只读工具Agent.md) | S4-lite：不依赖 LangGraph 的有界 R0 工具循环与 Citation |
| [roadmap/chat-to-agent/personal/S5-仓库感知Agent.md](roadmap/chat-to-agent/personal/S5-仓库感知Agent.md) | S5-lite：固定 commit 的本地只读代码检索和行号引用 |
| [roadmap/chat-to-agent/personal/可选升级触发条件.md](roadmap/chat-to-agent/personal/可选升级触发条件.md) | S3/S6–S9 何时值得重新评审；不构成实施卡 |
| [roadmap/chat-to-agent/00-执行约定与公共契约.md](roadmap/chat-to-agent/00-执行约定与公共契约.md) | 默认路线只使用 Tool/Citation/Event 子集；Run/Artifact/Approval 为平台参考 |
| [roadmap/chat-to-agent/01-稳定Chat基线.md](roadmap/chat-to-agent/01-稳定Chat基线.md) | 当前 Chat 基线技术附录；实施以 current-release/C1 为唯一来源 |
| [roadmap/chat-to-agent/02-项目作用域隔离.md](roadmap/chat-to-agent/02-项目作用域隔离.md) | 完整平台 S1 参考；不是个人默认实施卡 |
| [roadmap/chat-to-agent/03-Graph前分层重构.md](roadmap/chat-to-agent/03-Graph前分层重构.md) | 完整平台 S2 参考；不是个人默认实施卡 |
| [roadmap/chat-to-agent/04-确定性LangGraph.md](roadmap/chat-to-agent/04-确定性LangGraph.md) | S3 条件型平台参考；个人默认不实施 |
| [roadmap/chat-to-agent/05-只读工具Agent.md](roadmap/chat-to-agent/05-只读工具Agent.md) | 完整平台 S4 参考；个人默认使用精简卡 |
| [roadmap/chat-to-agent/06-仓库感知Agent.md](roadmap/chat-to-agent/06-仓库感知Agent.md) | 完整平台 S5 参考；个人默认使用精简卡 |
| [roadmap/chat-to-agent/07-可恢复AgentRun.md](roadmap/chat-to-agent/07-可恢复AgentRun.md) | 条件型 S6 参考：持久 Run、队列、检查点和恢复 |
| [roadmap/chat-to-agent/08-沙箱补丁Agent.md](roadmap/chat-to-agent/08-沙箱补丁Agent.md) | 条件型 S7 参考：安全物化、补丁与隔离验证 |
| [roadmap/chat-to-agent/09-审批式行动Agent.md](roadmap/chat-to-agent/09-审批式行动Agent.md) | 条件型 S8 参考：精确审批和本地 Git 行动 |
| [roadmap/chat-to-agent/10-生态集成与多Agent.md](roadmap/chat-to-agent/10-生态集成与多Agent.md) | 条件型 S9 参考：按子卡触发 Git/MCP/身份/多 Agent |
| [roadmap/chat-to-agent/验收证据模板.md](roadmap/chat-to-agent/验收证据模板.md) | 每阶段必须提交的测试、演示、指标、回滚和交接证据模板 |
| [migration/Python重构迁移文档.md](migration/Python重构迁移文档.md) | Java → Python 历史迁移记录（含 ADR 索引；不再直接下发任务） |
| [migration/testing-notes.md](migration/testing-notes.md) | 测试与集成踩坑留痕（langchain 导入 hang / test_migration 性能 / 异步客户端 loop） |
| [migration/后续升级计划.md](migration/后续升级计划.md) | 历史缺口和旧 U1–U5 预留，仅作背景；实施以 current-release 与个人路线为准 |
| [decisions/adr/](decisions/adr/) | 7 份架构决策记录 0001~0007 |
| [decisions/glossary.md](decisions/glossary.md) | 领域术语表（10 术语全 settled） |
| [integration/deepseek-notes.md](integration/deepseek-notes.md) | DeepSeek thinking/非思考模式集成约定 |
| [interview/面试准备指南.md](interview/面试准备指南.md) | 面试讲解与追问话术 |

## ADR 速查

| ADR | 决策 |
|-----|------|
| [0001](decisions/adr/0001-memory-vs-knowledge-two-tables-shared-recall.md) | Memory/Knowledge 分表 + 共享 VectorRecallService |
| [0002](decisions/adr/0002-knowledge-document-parent-child.md) | Knowledge 拆 documents+knowledge_chunks 父子表 |
| [0003](decisions/adr/0003-message-store-pg-source-of-truth.md) | 消息 PG 真相源 + Redis 缓存 + fallback + 摘要持久化 |
| [0004](decisions/adr/0004-memory-concept-and-conversation-naming.md) | Memory 紧定义 + conversation_id 命名 |
| [0005](decisions/adr/0005-prompttemplate-versioning-activation-chat.md) | PromptTemplate 版本化 + 每 type 恰一激活 + CHAT 纳入模板 |
| [0006](decisions/adr/0006-records-audit-log-merge.md) | Record=审计日志 + CR/UT 合并 ai_operation_records |
| [0007](decisions/adr/0007-core-domain-and-bounded-contexts.md) | 核心域=Chat，基建支撑子域，工具次要上下文 |
| [0008](decisions/adr/0008-document-parsing-element-aware-serialization.md) | 文档解析元素感知序列化（C5）：Title->`#`、PDF pdfminer 字号标题、扫描版拒绝 |
| [0009](decisions/adr/0009-reranker-deferred.md) | Reranker 二阶段重排--先暂缓（torch 约束），后 ONNX Runtime 重新评估落地（MRR 0.883→0.941） |
| [0010](decisions/adr/0010-chat-references-and-reasoning.md) | Chat 引用与思考过程增强（context.references + reasoning.delta SSE 事件，切 ChatDeepSeek） |
| [0011](decisions/adr/0011-jieba-chinese-bm25-segmentation.md) | jieba 中文分词：chunk_content_segmented 列 + CJK 先分词，中文 R@5 0.25→1.000 |
| [0012](decisions/adr/0012-topk-sensitivity-keep-5.md) | top_k 敏感性分析：60 golden 扫描 3-15，R@5 饱和、MRR 无单调，保持 5 |
| [0013](decisions/adr/0013-document-management-soft-delete.md) | 文档管理：软删行 + 物理删分块 + 列表 + replace 更新 |
| [0014](decisions/adr/0014-langchain-thin-adapter-no-langgraph.md) | LangChain 薄 adapter：仅 config.py 一处 import，完整 Agent 不引入 |
| [0015](decisions/adr/0015-langgraph-retrieval-enhancement.md) | LangGraph 检索增强：智能路由 + 自我纠错（路由准确率 60/60，决策变更见 ADR-0014） |
| [0016](decisions/adr/0016-react-agent-evaluation.md) | ReAct Agent 升级评估：thinking tool calling 原型验证通过，当前不实施完整 ReAct（留重启条件） |
