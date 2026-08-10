"""RagService - RAG 检索增强生成（ADR-0001/0002）。

上传知识文档 -> SemanticChunker 分块 -> VectorRecallService 内联向量化 -> 父子表存储；
检索 -> QueryRewriter 多查询改写 -> HybridRetriever 混合检索 -> 去重 -> 知识注入。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.infra.vector_recall import VectorRecallService
from app.ai.rag.chinese_segmenter import segment_chinese
from app.ai.rag.hybrid_retriever import HybridRetriever, ScoredChunk
from app.ai.rag.reranker import RerankerPort
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.rag.semantic_chunker import SemanticChunker
from app.models import Document, KnowledgeChunk


@dataclass(frozen=True)
class PreparedSearchQuery:
    text: str
    vector: list[float]


class RagService:
    def __init__(
        self,
        session: AsyncSession,
        chunker: SemanticChunker,
        vector_recall: VectorRecallService,
        query_rewriter: QueryRewriter,
        hybrid_retriever: HybridRetriever,
        reranker: RerankerPort | None = None,
    ) -> None:
        self.session = session
        self.chunker = chunker
        self.vector_recall = vector_recall
        self.query_rewriter = query_rewriter
        self.hybrid_retriever = hybrid_retriever
        self.reranker = reranker

    async def upload_document(
        self,
        title: str,
        content: str,
        source_type: str = "MANUAL",
        project_name: str | None = None,
        content_type: str = "md",
        async_mode: bool = False,
    ) -> Document:
        """上传知识文档：先完成外部 embedding，再以短事务写父子表。

        async_mode=True 时，只存父文档并提交 Celery 任务，分块和 embedding 异步执行。
        """
        if async_mode:
            doc = Document(title=title, source_type=source_type, project_name=project_name, content=content)
            self.session.add(doc)
            await self.session.flush()
            # P0-4: commit 后再派发，避免 Celery worker 读到未提交 doc（主事务未提交时 MVCC 不可见）
            await self.session.commit()
            from app.ai.tasks.document_parse import parse_document_task
            async_result = parse_document_task.delay(doc.id, title, content, source_type, project_name)
            doc._task_id = async_result.task_id
            return doc
        chunks = self.chunker.chunk(content, content_type=content_type)
        prepared_chunks = [
            (chunk_text, await self.vector_recall.embed(chunk_text))
            for chunk_text in chunks
        ]
        doc = Document(title=title, source_type=source_type, project_name=project_name, content=content)
        self.session.add(doc)
        await self.session.flush()
        for i, (chunk_text, embedding) in enumerate(prepared_chunks):
            kc = KnowledgeChunk(
                document_id=doc.id,
                chunk_index=i,
                chunk_content=chunk_text,
                chunk_content_segmented=segment_chinese(chunk_text),
            )
            await self.vector_recall.store_preembedded(self.session, kc, embedding)
        return doc

    async def search(self, query: str, top_k: int = 5) -> list[ScoredChunk]:
        """多查询改写 -> 预生成全部向量 -> 纯 DB 检索 -> 去重 -> Top-K。

        所有外部 LLM/embedding await 都发生在第一条 SQL 之前，避免多 query 循环在
        前一次检索已开启事务后继续等待下一次 embedding。
        """
        prepared = await self.prepare_search(query)
        return await self.search_prepared(prepared, top_k=top_k, rerank_query=query)

    async def prepare_search(self, query: str) -> list[PreparedSearchQuery]:
        """纯外部调用阶段：改写查询并生成全部向量，不执行 SQL。"""
        queries = await self.query_rewriter.rewrite(query)
        return [
            PreparedSearchQuery(text=q, vector=await self.vector_recall.embed(q))
            for q in queries
        ]

    async def search_prepared(
        self, queries: list[PreparedSearchQuery], top_k: int = 5,
        rerank_query: str | None = None,
    ) -> list[ScoredChunk]:
        """纯数据库阶段：消费预生成向量并完成混合召回与去重。

        rerank_query 提供时启用 reranker：扩大候选池（top_k*4）→ 去重 → 语义精排 → top_k。
        """
        from app.core.config import settings

        use_rerank = (
            self.reranker is not None
            and rerank_query is not None
            and settings.reranker_enabled
        )
        # 粗排候选池：rerank 开启时用 reranker_top_n（可配置，默认 20），否则 top_k*2
        pool_k = settings.reranker_top_n if use_rerank else top_k * 2
        seen: set[int] = set()
        all_results: list[ScoredChunk] = []
        for query in queries:
            results = await self.hybrid_retriever.search_by_vector(
                query.text, query.vector, top_k=pool_k
            )
            for r in results:
                if r.chunk.id not in seen:
                    seen.add(r.chunk.id)
                    all_results.append(r)
        if use_rerank:
            return await self.reranker.rerank(rerank_query, all_results, top_k)
        all_results.sort(key=lambda x: x.score, reverse=True)
        return all_results[:top_k]

    @staticmethod
    def format_context(results: list[ScoredChunk]) -> str:
        """纯函数：ScoredChunk 列表 -> 检索结果 markdown（不需 session）。

        P0-2: 改为静态方法，避免调用方为 format_context 持有 DB session。
        """
        if not results:
            return ""
        parts = ["## 相关知识库文档\n"]
        for i, r in enumerate(results):
            parts.append(f"### 文档{i + 1} (相关度:{r.score:.2f}, 来源:{r.match_type})\n{r.chunk.chunk_content}\n")
        return "\n".join(parts)

    async def delete_document(self, doc_id: int) -> None:
        """软删文档（ADR-0013）：标 status=DELETED + 物理删 chunks（释放向量存储）。

        documents 行保留（列表可审计/可追溯），chunks 删除后检索不再命中。
        """
        from sqlalchemy import delete as sa_delete

        from app.core.exceptions import BusinessException

        doc = await self.session.get(Document, doc_id)
        if doc is None or doc.status == "DELETED":
            # 不存在或已软删都视为 404（幂等删除语义）
            raise BusinessException("KNOWLEDGE_DOCUMENT_NOT_FOUND", status_code=404)
        doc.status = "DELETED"
        doc.deleted_at = datetime.now()  # DateTime 无时区列
        # 物理删 chunks（符合"删除文档 = 删该文档全部分块"策略）
        await self.session.execute(
            sa_delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id)
        )
        await self.session.flush()

    async def replace_document(
        self,
        doc_id: int,
        title: str,
        content: str,
        source_type: str = "DOC",
        project_name: str | None = None,
        content_type: str = "md",
    ) -> Document:
        """更新文档（ADR-0013）：软删旧文档 + 上传新文档（新 doc_id，ACTIVE）。"""
        await self.delete_document(doc_id)
        return await self.upload_document(
            title, content, source_type=source_type, project_name=project_name, content_type=content_type
        )
