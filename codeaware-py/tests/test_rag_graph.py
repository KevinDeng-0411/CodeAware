"""LangGraph 检索增强测试：router / evaluator / graph 集成（mock，无需真实 API）。"""

import pytest

from app.ai.rag.evaluator import RetrievalEvaluator
from app.ai.rag.rag_graph import RagGraph
from app.ai.rag.router import RouteRouter


class _FakeChunk:
    def __init__(self, doc_id, content):
        self.id = doc_id  # ScoredChunk 检索去重用
        self.document_id = doc_id
        self.chunk_content = content


class _Scored:
    def __init__(self, score, chunk=None, match_type="vector"):
        self.score = score
        self.match_type = match_type
        self.chunk = chunk or _FakeChunk(1, "test content")


# ---------- Evaluator 单测 ----------

async def test_evaluator_with_both_legs_is_satisfied():
    """命中查询：两腿叠加（both）-> 满意，不重试。"""
    ev = RetrievalEvaluator()
    assert await ev.evaluate([_Scored(0.03, match_type="both"),
                              _Scored(0.03, match_type="both"),
                              _Scored(0.016, match_type="vector")]) is True


async def test_evaluator_all_single_leg_is_unsatisfied():
    """无关/弱检索：全纯 vector（无 both/keyword）-> 不满意（触发重写）。"""
    ev = RetrievalEvaluator()
    assert await ev.evaluate([_Scored(0.016, match_type="vector"),
                              _Scored(0.016, match_type="vector"),
                              _Scored(0.016, match_type="vector")]) is False


async def test_evaluator_low_recall_is_unsatisfied():
    ev = RetrievalEvaluator()
    assert await ev.evaluate([_Scored(0.3), _Scored(0.2)]) is False


# ---------- Router 单测 ----------

class _FakeRouteLLM:
    def __init__(self, route):
        self._route = route

    def with_structured_output(self, schema, **kw):
        owner = self

        class _Structured:
            async def ainvoke(self, prompt):
                import json

                return schema.model_validate_json(json.dumps({"route": owner._route}))

        return _Structured()


async def test_router_decides_retrieve_and_direct():
    router = RouteRouter(_FakeRouteLLM("retrieve"))
    assert await router.decide("缓存击穿如何解决") == "retrieve"
    router2 = RouteRouter(_FakeRouteLLM("direct"))
    assert await router2.decide("今天天气怎么样") == "direct"


async def test_router_degrades_to_retrieve_on_failure():
    class _BrokenLLM:
        def with_structured_output(self, schema, **kw):
            class _S:
                async def ainvoke(self, prompt):
                    raise RuntimeError("llm down")

            return _S()

    router = RouteRouter(_BrokenLLM())
    # 宁可多检索不漏：失败降级 retrieve
    assert await router.decide("任何问题") == "retrieve"


# ---------- RagGraph 集成（mock） ----------

class _FakeSearchEngine:
    """可编排检索结果的 fake：按查询次数返回不同结果。"""

    def __init__(self):
        self.attempts = 0
        self.router_route = "retrieve"

    async def search(self, query):
        self.attempts += 1
        if self.attempts == 1:
            # 全纯 vector（无 both/keyword）-> 弱检索 -> 触发重试
            return [_Scored(0.016, match_type="vector"), _Scored(0.016, match_type="vector"),
                    _Scored(0.016, match_type="vector")]
        # 重试后命中 -> 含 both -> 满意
        return [_Scored(0.03, match_type="both"), _Scored(0.03, match_type="both"),
                _Scored(0.016, match_type="vector")]


class _FakeRewriteLLM:
    async def ainvoke(self, prompt):
        class _R:
            content = '["缓存 击穿 解决方案 布隆过滤器"]'
        return _R()


def _make_graph(engine, reranker=None):
    from app.ai.rag.query_rewriter import QueryRewriter

    class _FakeVR:
        async def embed(self, text):
            return [0.0] * 16

    class _FakeRetriever:
        async def search_by_vector(self, text, vector, top_k=10):
            return await engine.search(text)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **kw):
            class _R:
                def all(self):
                    return []
            return _R()

    def session_factory():
        return _FakeSession()

    # router 用 _FakeRouteLLM（控制 route）；rewriter 用 _FakeRewriteLLM（JSON 数组）
    return RagGraph(
        chat_model=_FakeRouteLLM(engine.router_route),
        vector_recall=_FakeVR(),
        lexical_recall=None,
        query_rewriter=QueryRewriter(_FakeRewriteLLM()),
        session_factory=session_factory,
        retriever_factory=lambda _s: _FakeRetriever(),
        reranker=reranker,
    )


async def test_graph_retries_on_unsatisfied_then_satisfies():
    engine = _FakeSearchEngine()
    graph = _make_graph(engine)
    result = await graph.run("缓存击穿")
    assert result.retries == 1  # 第一次模糊 -> 重试 1 次
    assert result.route == "retrieve"
    assert not result.direct
    assert result.docs  # 重试后命中


class _FakeReranker:
    """记录 rerank 调用（P0-1 验证 graph 路径接入精排）。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def rerank(self, query, candidates, top_k):
        self.calls.append({"query": query, "top_k": top_k, "n_candidates": len(candidates)})
        return candidates[:top_k]


async def test_graph_uses_reranker_on_retrieve():
    """P0-1: graph 路径应接入 reranker（_rag_node 传 rerank_query → use_rerank=True）。"""
    engine = _FakeSearchEngine()
    reranker = _FakeReranker()
    graph = _make_graph(engine, reranker=reranker)
    result = await graph.run("缓存击穿")
    assert reranker.calls, "graph 路径应调用 reranker（此前绕过精排）"
    assert reranker.calls[0]["query"] == "缓存击穿"
    assert reranker.calls[0]["top_k"] == 5
    assert result.route == "retrieve"


async def test_graph_direct_path_skips_retrieval():
    engine = _FakeSearchEngine()
    engine.router_route = "direct"
    graph = _make_graph(engine)
    result = await graph.run("今天天气怎么样")
    assert result.direct is True
    assert result.context == ""
