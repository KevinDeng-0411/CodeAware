# CLAUDE.md - CodeAware

AI 驱动的研发效能平台（软件工程实验室团队）：代码评审、新人培训、团队知识检索。
核心域 = **双模式 Chat**（`CHAT_MODE=rag|agent`）：RAG 混合检索问答 + ReAct Agent 工具循环。

## 当前状态（2026-08-11）

- **双模式 Chat**：RAG 模式（默认，确定性状态机）+ Agent 模式（CHAT_MODE=agent，ReAct 工具循环，v1.1.0+）
- **检索地基**：BM25（ParadeDB pg_search）+ jieba + pgvector + RRF 粗排（候选池 20）→ ONNX reranker 精排 top5（MRR 0.941）
- **Agent**：5 工具（search_knowledge / get_document / list_documents / calculate / get_current_time）；停止判断 = 模型自评 + 检索收敛检测 + per-tool 上限；eval 门禁过（recall 1.0 / closure 1.0 / direct 1.0）
- **LLMOps 闭环（ADR-0017）**：`agent_runs` run trace（回放端点）+ 失败沉淀（review → eval 回归集 sync）+ 请求边界 guardrail + memory 两 counter + 前端 Agent Runs 页（列表/流程视图/评审/三处跳转）
- **异步**：Celery（文档解析/记忆抽取）+ Kafka（audit/metrics）
- **P0 收口完成**：Graph 路径恢复精排、session 生命周期、任务幂等/派发、会话归属
- 全量测试 **350 passed**（+15 agent_ops）；前端 53 passed

## 文档索引（编码前先查）

| 场景 | 位置 |
|---|---|
| **总索引**（功能 → ADR → 章节） | [docs/INDEX.md](docs/INDEX.md) |
| **开发规则 / 编码铁律 / 测试门禁** | [AGENTS.md](AGENTS.md) |
| **架构决策**（17 篇 ADR 0001~0017） | [docs/decisions/adr/](docs/decisions/adr/) |
| 检索/Agent 评估（golden / rerank / agent-eval） | [docs/optimization/](docs/optimization/README.md) |
| AgentOps 观测/回放/失败沉淀 | [ADR-0017](docs/decisions/adr/0017-agent-llmops-closed-loop.md) |
| Chat 全链路 / 数据模型 / 契约 | [docs/roadmap/current-release/README.md](docs/roadmap/current-release/README.md) |
| DeepSeek 集成约定 | [docs/integration/deepseek-notes.md](docs/integration/deepseek-notes.md) |
| 面试文档 | [docs/interview/](docs/interview/) |

## 关键约束（不可破坏）

- **PG 是真相，Redis 只做缓存**；模型/embedding 等待期间**不持有 DB 事务**（短事务）
- **typed SSE 10 事件**（protocol_version=1，sequence 严格递增，前端 fail-closed 校验）
- **检索地基一个字节不改**：BM25/jieba/RRF/reranker 是基础设施，Agent 只在其上加决策层
- **guardrail 在请求边界**（ChatRequest validator，fail-closed）；**不在工具结果层做注入检测**（误报伤理解）
- **agent trace 默认只存元数据**：完整 reasoning 需 `AGENT_TRACE_INCLUDE_REASONING=true`
- **失败沉淀**：needs_review → 前端评审 accepted → `scripts/sync_regression_cases.py` 进 eval 回归集
- 向量只走 `VectorRecallService`；Knowledge 父子表（documents + knowledge_chunks）
- 测试必须过 `scripts/run_tests_safe.py`（fail-closed disposable 环境），**禁裸 pytest**
- LLM 默认 mock；真实 DeepSeek/Ollama 评估标 `live_eval` 显式运行

## 工作方式

- **发现问题当即修复**（带测试），不严格遵循 chat-to-agent 规划文档——它仅作方向参考，不是硬门禁
- 改动核心模块（rag/memory/chat/agent）→ 跑全量回归 + 相关定向测试；改动 Agent 工具/循环 → 重跑 `test_agent_eval.py`（live_eval）
- 架构决策写 ADR（编号延续 0016+）；当前实现以 `current-release/` 为执行权威
- 全异步（async SQLAlchemy/redis）；端点绝不返回裸 ORM（Pydantic 投影）

## 常用命令

```bash
# 测试（安全门禁，禁裸 pytest）
cd codeaware-py && uv run python scripts/run_tests_safe.py -q          # 全量
uv run python scripts/run_tests_safe.py tests/test_rag_graph.py -q    # 定向
uv run python scripts/run_tests_safe.py tests/eval/test_agent_eval.py -m live_eval -q  # Agent 真实评估

# 启动（推荐一键：基础服务 + native Celery worker + 后端 + 前端 + admin 账号）
./start.sh
# 手动分步（worker 缺失 → 上传分块/记忆抽取异步任务不执行，chunk_count=0！）
docker compose up -d postgres redis        # 基础服务
cd codeaware-py && uv run alembic upgrade head
uv run celery -A app.ai.celery_app worker --loglevel=warning   # Celery worker（必需）
uv run uvicorn app.main:app --port 8000    # 后端（CHAT_MODE=agent 环境变量切 Agent 模式）
cd frontend && npm run dev                 # 前端
# 健康检查：/health/ready 三态（ready/degraded/not_ready），checks 含 celery——worker 缺失显示 degraded
curl http://localhost:8000/health/ready
```
