"""P0-3: document.parse 异步任务幂等性——已分块文档重复处理不重复创建 chunk。"""

import pytest
from sqlalchemy import delete, func, select

from app.db.session import AsyncSessionLocal
from app.models import Document, KnowledgeChunk


@pytest.fixture(autouse=True)
async def _cleanup_documents_after_test(db_session):
    """测试后清理残留：commit 的数据会污染共享测试库（影响后续 knowledge/vector 测试）。"""
    yield
    async with AsyncSessionLocal() as s:
        await s.execute(delete(KnowledgeChunk))
        await s.execute(delete(Document))
        await s.commit()


async def test_document_parse_idempotent_when_already_chunked(setup_db):
    """同一 doc 已分块时再次执行任务应返回 already_chunked，不重复创建 chunk。"""
    async with AsyncSessionLocal() as s:
        doc = Document(title="缓存规范", content="# 缓存\n互斥锁", source_type="MANUAL")
        s.add(doc)
        await s.flush()
        s.add(KnowledgeChunk(
            document_id=doc.id, chunk_index=0,
            chunk_content="互斥锁", chunk_content_segmented="互斥锁",
        ))
        await s.commit()
        doc_id = doc.id

    from app.ai.tasks.document_parse import _parse_document

    # 直接 await 模块级核心（测试 loop + 测试库），不经 Celery task 的独立 event loop
    result = await _parse_document(doc_id, "缓存规范", "# 缓存\n互斥锁", "MANUAL", None)
    assert result["reason"] == "already_chunked"
    assert result["chunk_count"] == 0

    async with AsyncSessionLocal() as s:
        n = await s.scalar(
            select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.document_id == doc_id)
        )
    assert n == 1  # chunk 数不变（幂等）
