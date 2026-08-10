"""RagGraph - LangGraph 检索增强：智能路由 + 自我纠错。

StateGraph 节点：
- router_node: LLM 判断 route ∈ {retrieve, direct}（direct 跳过检索直接回答）
- rag_node: prepare_search + search_prepared（复用 RagService，短事务）
- evaluate_node: 极差 + 数量检测（RetrievalEvaluator）
- rewrite_node: QueryRewriter 二次改写（带失败信息 + 防打转铁律 + seen 兜底）

收敛性：MAX_RETRY=2 + seen_queries 重复立即跳出（库里没有时不再无谓检索）。
"""

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.ai.rag.evaluator import RetrievalEvaluator
from app.ai.rag.hybrid_retriever import ScoredChunk
from app.ai.rag.router import RouteRouter
from app.ai.rag.semantic_chunker import SemanticChunker
from app.ai.services.rag import RagService

logger = logging.getLogger(__name__)

MAX_RETRY = 2
SIM_THRESHOLD = 0.8  # 改写结果与上轮相似度 > 0.8 视为"原地打转"，强制换角度


@dataclass
class RagResult:
    route: str                      # retrieve / direct
    docs: list[ScoredChunk] = field(default_factory=list)
    context: str = ""               # format_context 结果（空 = 未检索或无结果）
    refs: list[dict] = field(default_factory=list)
    retries: int = 0
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    direct: bool = False            # 是否走了 direct 路径（跳过检索）


class RagState(TypedDict, total=False):
    message: str
    route: str
    queries: list[str]              # 尝试过的查询（首次 = message）
    retries: int
    docs: list[ScoredChunk]
    satisfied: bool
    should_stop: bool               # seen 兜底：query 重复 -> 立即结束
    result: RagResult


def _similar(a: str, b: str) -> float:
    """字符级相似度（防打转检测，比 embedding 成本低得多）。"""
    return SequenceMatcher(None, a, b).ratio()


class RagGraph:
    def __init__(
        self,
        *,
        chat_model,
        vector_recall,
        lexical_recall,
        query_rewriter,
        chunker: SemanticChunker | None = None,
        session_factory=None,
        retriever_factory=None,
        reranker=None,
    ) -> None:
        self.chat_model = chat_model
        self.vector_recall = vector_recall
        self.lexical_recall = lexical_recall
        self.query_rewriter = query_rewriter
        self.chunker = chunker or SemanticChunker()
        self.session_factory = session_factory
        self.reranker = reranker
        # retriever_factory(session) -> HybridRetriever；测试注入 fake，生产默认真实
        self.retriever_factory = retriever_factory
        self.router = RouteRouter(chat_model)
        self.evaluator = RetrievalEvaluator()
        self._app = self._build()

    def _make_retriever(self, session):
        if self.retriever_factory is not None:
            return self.retriever_factory(session)
        from app.ai.rag.hybrid_retriever import HybridRetriever

        return HybridRetriever(session, self.vector_recall, self.lexical_recall)

    def _build(self):
        g = StateGraph(RagState)
        g.add_node("router", self._router_node)
        g.add_node("rag", self._rag_node)
        g.add_node("evaluate", self._evaluate_node)
        g.add_node("rewrite", self._rewrite_node)
        g.add_edge(START, "router")
        # router: direct -> 结束（RagResult.direct=True）；retrieve -> rag
        g.add_conditional_edges(
            "router",
            lambda s: END if s["route"] == "direct" else "rag",
            {"rag": "rag", END: END},
        )
        g.add_edge("rag", "evaluate")
        # evaluate: 满意/达上限/兜底 -> 结束；否则 -> rewrite
        g.add_conditional_edges(
            "evaluate",
            self._route_after_eval,
            {"rewrite": "rewrite", END: END},
        )
        g.add_edge("rewrite", "rag")
        return g.compile()

    def _route_after_eval(self, state: RagState) -> str:
        if state.get("satisfied") or state.get("should_stop"):
            return END
        if state["retries"] >= MAX_RETRY:
            return END
        return "rewrite"

    # ---------- 节点 ----------

    async def _router_node(self, state: RagState) -> dict:
        route = await self.router.decide(state["message"])
        return {"route": route, "queries": [state["message"]], "retries": 0}

    async def _rag_node(self, state: RagState) -> dict:
        """检索：短事务 + 复用 RagService.prepare_search + search_prepared。

        P0-1: 传入 rerank_query 启用 ONNX 精排（粗排候选池 20 → 精排 top5，与 service 路径一致）。
        P0-2: session 用 async with 生命周期管理（此前泄漏连接池）。
        """
        query = state["queries"][-1]
        async with await self._session() as session:
            hybrid = self._make_retriever(session)
            rag = RagService(
                session,
                self.chunker,
                self.vector_recall,
                self.query_rewriter,
                hybrid,
                self.reranker,
            )
            prepared = await rag.prepare_search(query)
            docs = await rag.search_prepared(prepared, top_k=5, rerank_query=query)
        return {"docs": docs, "satisfied": False}

    async def _evaluate_node(self, state: RagState) -> dict:
        satisfied = await self.evaluator.evaluate(state["docs"])
        return {"satisfied": satisfied}

    async def _rewrite_node(self, state: RagState) -> dict:
        """二次改写（带失败信息 + 防打转 + seen 兜底）。"""
        prev = state["queries"][-1]
        original = state["message"]
        try:
            new_queries = await self.query_rewriter.rewrite(
                prev, failure_hint="上次检索未命中，请从不同角度改写，避免与上次表述重复"
            )
            new_query = new_queries[0]
        except Exception as exc:
            logger.warning("rag graph rewrite failed type=%s", type(exc).__name__)
            return {"should_stop": True}

        # 防打转：与上一轮太相似 -> 强制回到原问题不同表述（或直接停）
        if _similar(new_query, prev) > SIM_THRESHOLD:
            logger.debug("rewrite similar to prev (%.2f), trying original-based rewrite", _similar(new_query, prev))
            try:
                alt = await self.query_rewriter.rewrite(
                    original, failure_hint="请用完全不同的表述重新组织该问题"
                )
                new_query = alt[0]
            except Exception:
                return {"should_stop": True}

        # seen 兜底：query 已在尝试列表 -> 库里没有，立即结束
        if new_query in state["queries"]:
            return {"should_stop": True}

        return {
            "queries": state["queries"] + [new_query],
            "retries": state["retries"] + 1,
            "should_stop": False,
        }

    async def _session(self):
        """返回 AsyncSession。session_factory 供测试注入；默认 AsyncSessionLocal。"""
        if self.session_factory is not None:
            return self.session_factory()
        from app.db.session import AsyncSessionLocal

        return AsyncSessionLocal()

    # ---------- 入口 ----------

    async def run(self, message: str) -> RagResult:
        """执行图，返回 RagResult。"""
        try:
            state = await self._app.ainvoke(
                {"message": message, "queries": [], "retries": 0, "satisfied": False}
            )
        except Exception as exc:
            logger.warning("rag graph run failed type=%s", type(exc).__name__)
            # 图异常：降级为未检索（direct 语义），不破坏 Chat
            return RagResult(route="retrieve", direct=False, retries=0,
                             warnings=[("rag_graph", "RAG_GRAPH_FAILED", "检索图执行失败，已降级")])

        result = RagResult(
            route=state.get("route", "retrieve"),
            docs=state.get("docs", []),
            retries=state.get("retries", 0),
        )
        # direct 路径：跳过检索
        if state.get("route") == "direct" and not state.get("docs"):
            result.direct = True
            result.context = ""
            result.warnings.append(("route", "ROUTE_DIRECT", "已判定为常识问题，未检索知识库"))
            return result
        # 检索路径：format_context + refs（对齐 turn_coordinator 现有逻辑）
        # P0-2: format_context 是纯函数（不需 session），移除冗余 session 构造（此前泄漏连接池）
        docs = result.docs
        result.context = RagService.format_context(docs)
        if docs:
            from sqlalchemy import select

            from app.models import Document

            async with await self._session() as s:
                doc_ids = {r.chunk.document_id for r in docs}
                titles = dict(
                    (await s.execute(
                        select(Document.id, Document.title)
                        .where(Document.id.in_(doc_ids))
                    )).all()
                )
            result.refs = [
                {
                    "document_id": r.chunk.document_id,
                    "title": titles.get(r.chunk.document_id, "未知文档"),
                    "snippet": r.chunk.chunk_content[:100],
                    "match_type": r.match_type,
                    "score": round(float(r.score), 4),
                }
                for r in docs
            ]
        # seen 兜底触发：query 重复 -> 未找到
        if state.get("should_stop") and not docs:
            result.warnings.append(("rag_graph", "RAG_NOT_FOUND", "知识库中未检索到相关信息"))
        return result
