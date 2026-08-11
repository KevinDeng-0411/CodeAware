"""ContextBuilder - 构建 Chat 上下文（记忆 + RAG + 历史）。

从 TurnCoordinator 提取，负责：
- 加载消息（Redis-first + PG fallback）
- 加载摘要（Redis-first + PG fallback）
- 长期记忆召回
- RAG 检索（LangGraph 或 service 路径）
- 组装 Prompt
"""

import logging

from sqlalchemy import select

from app.ai.memory.long_term import LongTermMemoryManager
from app.ai.memory.short_term import ShortTermMemoryManager, MessageEntry
from app.ai.rag.hybrid_retriever import HybridRetriever
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.rag.semantic_chunker import SemanticChunker
from app.ai.services.rag import RagService
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models import Document, LongTermMemory

logger = logging.getLogger(__name__)


def _emit_recall_metrics(cid: str, count: int) -> None:
    """Memory-Ops（ADR-0017）：记忆召回成功时发 recall counter（Kafka，best-effort）。

    emit_memory_metrics 内部对无 producer 静默 no-op，绝不影响调用方。
    """
    try:
        from app.ai.events.producer import emit_memory_metrics

        emit_memory_metrics(event_type="recall", conversation_id=cid, count=count)
    except Exception:  # noqa: BLE001
        logger.warning("memory recall metric emit failed conversation_id=%s", cid)


class ContextBuilder:
    def __init__(self, chat_model, redis_client, vector_recall, lexical_recall,
                 query_rewriter, chunker, reranker=None) -> None:
        self.chat_model = chat_model
        self.redis = redis_client
        self.vector_recall = vector_recall
        self.lexical_recall = lexical_recall
        self.query_rewriter = query_rewriter
        self.chunker = chunker
        self.reranker = reranker

    # ---------- 消息加载 ----------

    async def load_messages(self, cid: str) -> tuple[list[MessageEntry], bool]:
        """Redis-first + PG fallback。"""
        cache_failed = False
        try:
            async with AsyncSessionLocal() as s:
                st = ShortTermMemoryManager(self.redis, s, self.chat_model)
                messages = await st.read_cached_messages(cid)
        except Exception:
            messages = []
            cache_failed = True
        if messages:
            return messages, cache_failed

        async with AsyncSessionLocal() as s:
            st = ShortTermMemoryManager(self.redis, s, self.chat_model)
            messages = await st.read_recent_messages(cid)
        if not messages:
            return [], cache_failed

        try:
            async with AsyncSessionLocal() as s:
                st = ShortTermMemoryManager(self.redis, s, self.chat_model)
                await st.refill_message_cache(cid, messages)
        except Exception:
            cache_failed = True
        return messages, cache_failed

    # ---------- 摘要加载 ----------

    async def load_summary(self, cid: str) -> tuple[str | None, bool]:
        """Redis-first + PG fallback。"""
        cache_failed = False
        try:
            async with AsyncSessionLocal() as s:
                st = ShortTermMemoryManager(self.redis, s, self.chat_model)
                summary = await st.read_cached_summary(cid)
        except Exception:
            summary = None
            cache_failed = True
        if summary:
            return summary, cache_failed

        async with AsyncSessionLocal() as s:
            st = ShortTermMemoryManager(self.redis, s, self.chat_model)
            summary = await st.read_summary_from_pg(cid)
        if not summary:
            return None, cache_failed

        try:
            async with AsyncSessionLocal() as s:
                st = ShortTermMemoryManager(self.redis, s, self.chat_model)
                await st.refresh_summary_cache(cid, summary)
        except Exception:
            cache_failed = True
        return summary, cache_failed

    # ---------- 上下文构建 ----------

    async def build(
        self, cid, message, warn_callback
    ) -> tuple[str | None, list[tuple[str, str, str]], dict]:
        """构建 Chat context，返回 (prompt, warnings, refs)。

        warn_callback(component, code, msg) -> tuple[str, str, str]
        """
        warnings: list[tuple[str, str, str]] = []
        msgs, cache_failed = await self.load_messages(cid)
        if cache_failed:
            warnings.append(warn_callback(cid, "message_cache", "REDIS_UNAVAILABLE",
                                          "消息缓存回填失败，已使用 PostgreSQL 真相"))

        if msgs and msgs[-1].role == "USER" and msgs[-1].content == message:
            msgs = msgs[:-1]

        summary, summary_failed = await self.load_summary(cid)
        if summary_failed:
            warnings.append(warn_callback(cid, "summary_cache", "REDIS_UNAVAILABLE",
                                          "摘要缓存读取失败，已使用 PostgreSQL 真相"))

        history_parts = []
        if summary:
            history_parts.append(f"## 历史对话摘要\n{summary}")
        if msgs:
            history_parts.append(
                "## 最近对话\n" + "\n".join(f"{m.role}: {m.content}" for m in msgs)
            )
        history = "\n\n".join(history_parts)

        long_ctx = ""
        memory_refs: list[dict] = []
        try:
            memory_vector = await self.vector_recall.embed(message)
            async with AsyncSessionLocal() as s:
                recalled = await self.vector_recall.recall_by_vector(
                    s, LongTermMemory, message, memory_vector,
                    threshold=0.0, top_k=5,
                )
            if recalled:
                long_ctx = "\n".join(
                    f"- {memory[0].content} (相似度:{memory[1]:.2f})"
                    for memory in recalled
                )
                memory_refs = [
                    {
                        "content": memory[0].content,
                        "memory_type": memory[0].memory_type,
                        "similarity": round(float(memory[1]), 4),
                    }
                    for memory in recalled
                ]
                _emit_recall_metrics(cid, len(recalled))
        except Exception:
            warnings.append(warn_callback(cid, "memory_recall", "MEMORY_RECALL_FAILED",
                                          "长期记忆召回降级"))

        rag_ctx = ""
        knowledge_refs: list[dict] = []
        try:
            if settings.rag_runtime == "graph":
                from app.ai.rag.rag_graph import RagGraph

                graph = RagGraph(
                    chat_model=self.chat_model,
                    vector_recall=self.vector_recall,
                    lexical_recall=self.lexical_recall,
                    query_rewriter=self.query_rewriter,
                    chunker=self.chunker,
                    session_factory=AsyncSessionLocal,
                    reranker=self.reranker,
                )
                result = await graph.run(message)
                rag_ctx = result.context
                knowledge_refs = result.refs
                for component, code, msg in result.warnings:
                    warnings.append(warn_callback(cid, component, code, msg))
                if result.direct:
                    rag_ctx = ""
            else:
                async with AsyncSessionLocal() as s:
                    hybrid = HybridRetriever(s, self.vector_recall, self.lexical_recall)
                    rag = RagService(s, self.chunker, self.vector_recall, self.query_rewriter, hybrid, self.reranker)
                    prepared_queries = await rag.prepare_search(message)
                async with AsyncSessionLocal() as s:
                    hybrid = HybridRetriever(s, self.vector_recall, self.lexical_recall)
                    rag = RagService(s, self.chunker, self.vector_recall, self.query_rewriter, hybrid, self.reranker)
                    docs = await rag.search_prepared(prepared_queries, top_k=5, rerank_query=message)
                    rag_ctx = rag.format_context(docs)
                    if docs:
                        doc_ids = {r.chunk.document_id for r in docs}
                        titles = dict(
                            (await s.execute(
                                select(Document.id, Document.title)
                                .where(Document.id.in_(doc_ids))
                            )).all()
                        )
                        knowledge_refs = [
                            {
                                "document_id": r.chunk.document_id,
                                "title": titles.get(r.chunk.document_id, "未知文档"),
                                "snippet": r.chunk.chunk_content[:100],
                                "match_type": r.match_type,
                                "score": round(float(r.score), 4),
                            }
                            for r in docs
                        ]
        except Exception as exc:
            warnings.append(warn_callback(cid, "rag_retrieval", "RAG_RETRIEVAL_FAILED", "检索降级"))
            logger.warning("RAG retrieval failed conversation_id=%s error=%s", cid, exc)

        params = {
            "long_term_memory": long_ctx or "（无）",
            "rag_context": rag_ctx or "（无）",
            "conversation_history": history or "（无）",
            "user_message": message,
        }
        prompt = await self._render_prompt(params)
        if prompt is None:
            return None, warnings, {"knowledge_refs": [], "memory_refs": []}
        return prompt, warnings, {"knowledge_refs": knowledge_refs, "memory_refs": memory_refs}

    async def build_agent_messages(
        self, cid, message, warn_callback, system_prompt: str
    ):
        """Agent 模式（ADR-0016）：构造初始 LangChain messages。

        与 build() 复用数据加载（消息/摘要/长期记忆），但：
        - 跳过 RAG 预检索（交给 search_knowledge 工具，agent 自主决策）
        - 用 LangChain messages（System/Human/AI）而非字符串模板渲染
        返回 (messages, warnings, refs)。
        """
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        warnings: list[tuple[str, str, str]] = []
        msgs, cache_failed = await self.load_messages(cid)
        if cache_failed:
            warnings.append(warn_callback(cid, "message_cache", "REDIS_UNAVAILABLE",
                                          "消息缓存回填失败，已使用 PostgreSQL 真相"))
        if msgs and msgs[-1].role == "USER" and msgs[-1].content == message:
            msgs = msgs[:-1]

        summary, summary_failed = await self.load_summary(cid)
        if summary_failed:
            warnings.append(warn_callback(cid, "summary_cache", "REDIS_UNAVAILABLE",
                                          "摘要缓存读取失败，已使用 PostgreSQL 真相"))

        memory_refs: list[dict] = []
        long_ctx = ""
        try:
            memory_vector = await self.vector_recall.embed(message)
            async with AsyncSessionLocal() as s:
                recalled = await self.vector_recall.recall_by_vector(
                    s, LongTermMemory, message, memory_vector,
                    threshold=0.0, top_k=5,
                )
            if recalled:
                long_ctx = "\n".join(
                    f"- {memory[0].content} (相似度:{memory[1]:.2f})"
                    for memory in recalled
                )
                memory_refs = [
                    {
                        "content": memory[0].content,
                        "memory_type": memory[0].memory_type,
                        "similarity": round(float(memory[1]), 4),
                    }
                    for memory in recalled
                ]
                _emit_recall_metrics(cid, len(recalled))
        except Exception:
            warnings.append(warn_callback(cid, "memory_recall", "MEMORY_RECALL_FAILED",
                                          "长期记忆召回降级"))

        messages = [SystemMessage(content=system_prompt)]
        if summary:
            messages.append(SystemMessage(content=f"## 历史对话摘要\n{summary}"))
        for m in msgs:
            messages.append(
                HumanMessage(content=m.content)
                if m.role == "USER"
                else AIMessage(content=m.content)
            )
        if long_ctx:
            messages.append(SystemMessage(content=f"## 长期记忆\n{long_ctx}"))
        messages.append(HumanMessage(content=message))
        # 本轮上下文快照（ADR-0017）：短时记忆 = 增量摘要 + 消息窗口（只记 count，
        # 消息全文在 messages 表）；长时记忆 = memory_refs。供 run 落库观测。
        snapshot = {
            "summary": summary,
            "window": {"count": len(msgs)},
            "memory_refs": memory_refs,
            "knowledge_refs": [],
        }
        return messages, warnings, {"knowledge_refs": [], "memory_refs": memory_refs}, snapshot

    async def _render_prompt(self, params: dict) -> str | None:
        """渲染 CHAT 模板。"""
        from app.ai.prompt.template_manager import PromptTemplateManager
        from app.core.enums import PromptType

        try:
            async with AsyncSessionLocal() as s:
                pm = PromptTemplateManager(s)
                template = await pm.get_active(PromptType.CHAT)
                if template is None:
                    return None
                return pm.render_system_prompt(template, params)
        except Exception:
            return None