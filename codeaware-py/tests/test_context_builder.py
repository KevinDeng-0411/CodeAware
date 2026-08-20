"""ContextBuilder 长期记忆召回阈值（A 层修复：mem_recall_threshold）。

背景：conftest FakeEmbedder 产出的向量全正向未中心化，不同文本的余弦基线
≈0.72-0.84（非注释声称的近正交）——因此默认阈值 0.5 下过滤行为不可观测。
本测试把阈值提到 0.99：同文（sim=1.0）保留、异文（sim≈0.72）被滤，
验证调用点确实读取 settings.mem_recall_threshold 并执行过滤。
0.5 的具体取值属于生产策略，由真实 embedding（bge-m3）的相似度分布决定。
"""

from app.ai.prompt.template_manager import PromptTemplateManager
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.services.context_builder import ContextBuilder
from app.core.config import settings
from app.core.enums import PromptType
from app.models import LongTermMemory

_MEMORY_CONTENT = "C2-A 阈值测试记忆"


class _StubRagResult:
    context = ""
    refs = []
    warnings = []
    direct = False


class _StubRagGraph:
    """隔离 RAG 检索，只测记忆召回阈值。"""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, message):
        return _StubRagResult()


def _warn(cid, component, code, message):
    return component, code, message


def test_default_mem_recall_threshold():
    assert settings.mem_recall_threshold == 0.5


async def test_memory_recall_threshold_filters_dissimilar(
    db_session,
    redis_client,
    vector_recall,
    chunker,
    lexical_recall,
    mock_llm,
    monkeypatch,
):
    # 阈值提到 0.99，使 fake embedder 下的过滤可观测
    monkeypatch.setattr(settings, "mem_recall_threshold", 0.99)
    monkeypatch.setattr("app.ai.rag.rag_graph.RagGraph", _StubRagGraph)

    pm = PromptTemplateManager(db_session)
    await pm.save_and_activate(
        PromptType.CHAT,
        name="test-mem-threshold",
        role_setting="test",
        template_body="MEMORY:\n{{long_term_memory}}",
    )
    # 记忆存储时先 embed（真实流程同），否则 embedding=NULL 会被余弦过滤掉
    memory = LongTermMemory(content=_MEMORY_CONTENT, memory_type="FACT")
    memory.embedding = await vector_recall.embed(_MEMORY_CONTENT)
    db_session.add(memory)
    await db_session.commit()

    cb = ContextBuilder(
        mock_llm,
        redis_client,
        vector_recall,
        lexical_recall,
        QueryRewriter(mock_llm),
        chunker,
    )
    cid = "mem-threshold-cid"

    # 正例：同文查询 sim=1.0 ≥ 0.99 → 召回
    prompt, _, refs = await cb.build(cid, _MEMORY_CONTENT, _warn)
    assert _MEMORY_CONTENT in prompt
    assert [m["content"] for m in refs["memory_refs"]] == [_MEMORY_CONTENT]

    # 反例：异文查询 sim≈0.72 < 0.99 → 过滤（记忆段为空）
    prompt2, _, refs2 = await cb.build(cid, "完全无关的话题文本xyz", _warn)
    assert "（无）" in prompt2
    assert refs2["memory_refs"] == []
