"""文档解析+分块+embedding 异步任务。"""

import asyncio

from sqlalchemy import select

from app.ai.celery_app import celery_app
from app.ai.infra.vector_recall import VectorRecallService
from app.ai.rag.chinese_segmenter import segment_chinese
from app.ai.rag.semantic_chunker import SemanticChunker
from app.ai.tasks.base import CodeAwareTask
from app.db.session import AsyncSessionLocal
from app.models import Document, KnowledgeChunk


async def _parse_document(doc_id: int, title: str, content: str,
                          source_type: str = "MANUAL",
                          project_name: str | None = None) -> dict:
    """文档解析核心（模块级 async，供 Celery task 与测试直接调用）。

    P0-3 幂等：先检查 doc 是否已分块，避免 Celery 重试/重复派发重复创建 chunk。
    """
    async with AsyncSessionLocal() as session:
        doc = await session.get(Document, doc_id)
        if doc is None:
            raise ValueError(f"Document {doc_id} not found")
        existing = await session.scalar(
            select(KnowledgeChunk.id)
            .where(KnowledgeChunk.document_id == doc_id)
            .limit(1)
        )
        if existing is not None:
            return {"doc_id": doc_id, "chunk_count": 0, "reason": "already_chunked"}

    chunker = SemanticChunker()
    chunks = chunker.chunk(content, content_type="md")
    from app.ai.config import get_embedding_model
    vector_recall = VectorRecallService(get_embedding_model())
    prepared = []
    for chunk_text in chunks:
        embedding = await vector_recall.embed(chunk_text)
        prepared.append((chunk_text, embedding))
    async with AsyncSessionLocal() as session:
        for i, (chunk_text, embedding) in enumerate(prepared):
            kc = KnowledgeChunk(
                document_id=doc_id, chunk_index=i,
                chunk_content=chunk_text,
                chunk_content_segmented=segment_chinese(chunk_text),
            )
            await vector_recall.store_preembedded(session, kc, embedding)
        await session.commit()
    return {"doc_id": doc_id, "chunk_count": len(prepared)}


@celery_app.task(bind=True, base=CodeAwareTask, name="document.parse")
def parse_document_task(self, doc_id: int, title: str, content: str,
                        source_type: str = "MANUAL", project_name: str | None = None) -> dict:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _parse_document(doc_id, title, content, source_type, project_name)
        )
    finally:
        loop.close()
