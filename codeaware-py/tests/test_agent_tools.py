"""ADR-0016: Agent 工具集单元测试。

工具内部自管 AsyncSessionLocal（短 session，ADR-0003），测试需先 upload + commit
数据，工具新 session 才能读到。embedding 用 FakeEmbedder（确定性），CI 友好。
"""

import pytest

from app.ai.agent.tools import AgentToolkit


@pytest.fixture(autouse=True)
async def _cleanup_documents_after_test(db_session):
    """每个测试后清空文档残留。

    工具内部用独立 AsyncSessionLocal + 测试需 commit 数据才能被读到，已 commit 的
    数据 rollback 撤销不了，会污染共享测试库、影响后续假设库状态的测试（如
    knowledge_upload 字符上限、vector_recall 命中数）。autouse 保证本文件测试后清理。
    """
    yield
    from sqlalchemy import delete

    from app.models import Document, KnowledgeChunk

    await db_session.execute(delete(KnowledgeChunk))
    await db_session.execute(delete(Document))
    await db_session.commit()


@pytest.fixture
def toolkit(vector_recall, lexical_recall, chunker, mock_llm):
    from app.ai.rag.query_rewriter import QueryRewriter

    return AgentToolkit(vector_recall, lexical_recall, QueryRewriter(mock_llm), chunker, None)


def _tool_map(toolkit) -> dict:
    return {t.name: t for t in toolkit.get_tools()}


async def test_calculate_basic(toolkit):
    tools = _tool_map(toolkit)
    result = await tools["calculate"].ainvoke({"expression": "123 * 456 + 789"})
    assert result == "56877"


async def test_calculate_modulo(toolkit):
    tools = _tool_map(toolkit)
    result = await tools["calculate"].ainvoke({"expression": "2024 % 4"})
    assert result == "0"


async def test_calculate_rejects_code(toolkit):
    """防注入：表达式含导入/调用被拒绝。"""
    tools = _tool_map(toolkit)
    result = await tools["calculate"].ainvoke({"expression": "__import__('os').system('ls')"})
    assert "失败" in result


async def test_get_current_time(toolkit):
    from datetime import datetime

    tools = _tool_map(toolkit)
    result = await tools["get_current_time"].ainvoke({})
    assert str(datetime.now().year) in result


async def test_search_knowledge_finds_chunk(toolkit, rag_service, db_session):
    await rag_service.upload_document(
        "缓存最佳实践",
        "# 缓存\n## 击穿\n互斥锁 + 逻辑过期方案解决缓存击穿问题\n## 穿透\n布隆过滤器拦截空值",
        source_type="MANUAL", project_name="p",
    )
    await db_session.commit()  # 工具新 session 需读到已提交数据
    tools = _tool_map(toolkit)
    result = await tools["search_knowledge"].ainvoke({"query": "缓存击穿", "top_k": 3})
    assert "缓存击穿" in result


async def test_search_knowledge_no_hit_empty_kb(toolkit, db_session):
    """知识库为空时返回"未检索到相关内容"。

    显式清空（前面测试 db_session.commit() 的数据会残留在库中，rollback 撤销不了
    已提交事务）后再测，保证空库假设成立。
    """
    from sqlalchemy import delete

    from app.models import Document, KnowledgeChunk

    await db_session.execute(delete(KnowledgeChunk))
    await db_session.execute(delete(Document))
    await db_session.commit()
    tools = _tool_map(toolkit)
    result = await tools["search_knowledge"].ainvoke({"query": "缓存击穿", "top_k": 3})
    assert "未检索到相关内容" in result


async def test_get_document_returns_full_text(toolkit, rag_service, db_session):
    doc = await rag_service.upload_document(
        "部署手册",
        "# 部署\n## 步骤\n第一步启动 PostgreSQL\n第二步启动 Redis\n第三步启动应用",
        source_type="MANUAL", project_name="p",
    )
    await db_session.commit()
    tools = _tool_map(toolkit)
    result = await tools["get_document"].ainvoke({"document_id": doc.id})
    assert "部署手册" in result
    assert "PostgreSQL" in result


async def test_get_document_missing(toolkit):
    tools = _tool_map(toolkit)
    result = await tools["get_document"].ainvoke({"document_id": 999999})
    assert "不存在" in result


async def test_list_documents_lists_uploaded(toolkit, rag_service, db_session):
    await rag_service.upload_document(
        "接口规范", "# 接口\nRESTful 规范", "MANUAL", "p",
    )
    await rag_service.upload_document(
        "测试规范", "# 测试\npytest 规范", "MANUAL", "p",
    )
    await db_session.commit()
    tools = _tool_map(toolkit)
    result = await tools["list_documents"].ainvoke({"limit": 20})
    assert "接口规范" in result
    assert "测试规范" in result
