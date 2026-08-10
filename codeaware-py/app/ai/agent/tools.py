"""AgentToolkit - ReAct Agent 模式工具集（ADR-0016）。

设计原则：
- 工具不重复 ADR-0015 已做的检索决策，只暴露能力。
- 模型负责决策（何时查/查够没），工具负责专长（查得准/算得对）。
- 工具返回 `ToolObservation`（display 给模型看 + doc_ids 签名供 react_loop 收敛检测）；
  计算/时间等无文档工具返回 str（react_loop 兜底）。
- DB 工具内部自管短 session（AsyncSessionLocal），不跨模型调用持有事务（ADR-0003）。

工具集（与用户确定）：
1. search_knowledge  - chunk 级混合检索（保留 QueryRewriter + RRF + reranker，MRR 0.941 流水线）
2. get_document      - 文档级全文（截断 ~4000 字符），chunk 检索之外的文档浏览
3. list_documents    - 列出知识库文档
4. calculate         - 安全算术求值
5. get_current_time  - 当前时间

工具 docstring 是模型决策依据（何时用哪个），必须写清场景。
"""

import ast
import operator as _op
from dataclasses import dataclass, field
from datetime import datetime

from langchain_core.tools import tool
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models import Document, KnowledgeChunk


@dataclass
class ToolObservation:
    """工具返回值：展示文本（给模型）+ 文档 id 签名（供 react_loop 收敛检测）。

    doc_ids 空集合表示该结果不含知识库文档信息（如计算/时间，或检索无结果）。
    react_loop 用 doc_ids 判断"本轮是否带来新文档"，无新文档且检索工具被调用
    → 判定检索已收敛，强制进入终答（ADR-0016 停止判断优化）。
    """

    display: str
    doc_ids: frozenset[int] = field(default_factory=frozenset)

# observation 最大字符数：工具返回过长会撑爆上下文（LLM context 有限）
MAX_OBSERVATION_CHARS = 4000

_ALLOWED_BINOPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}


def _safe_eval(node: ast.AST) -> float:
    """受限算术求值：只允许数字 + 基本运算符，禁止名字/调用/属性（防注入）。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _safe_eval(node.left), _safe_eval(node.right)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（截断，共 {len(text)} 字符）"


class AgentToolkit:
    """Agent 模式工具集。注入检索依赖，生成可 bind_tools 的 LangChain 工具列表。"""

    def __init__(self, vector_recall, lexical_recall, query_rewriter,
                 chunker, reranker=None) -> None:
        self.vector_recall = vector_recall
        self.lexical_recall = lexical_recall
        self.query_rewriter = query_rewriter
        self.chunker = chunker
        self.reranker = reranker

    def get_tools(self) -> list:
        """返回 5 个工具（LangChain @tool，可 bind_tools）。"""
        vector_recall = self.vector_recall
        lexical_recall = self.lexical_recall
        query_rewriter = self.query_rewriter
        chunker = self.chunker
        reranker = self.reranker

        @tool
        async def search_knowledge(query: str, top_k: int = 5) -> ToolObservation:
            """在知识库中检索技术文档片段。

            当需要查找规范、设计文档、团队约定、代码说明、操作手册等知识时使用。
            返回相关文档片段的摘要列表（含相关度和来源）。若一次检索不充分，可换
            更具体的关键词再次调用。top_k 为返回条数（默认 5，最多 10）。
            """
            from app.ai.rag.hybrid_retriever import HybridRetriever
            from app.ai.services.rag import RagService

            top_k = max(1, min(top_k, 10))
            async with AsyncSessionLocal() as s:
                hybrid = HybridRetriever(s, vector_recall, lexical_recall)
                rag = RagService(s, chunker, vector_recall, query_rewriter, hybrid, reranker)
                results = await rag.search(query, top_k=top_k)
                context = rag.format_context(results)
            if not context:
                return ToolObservation(display="知识库中未检索到相关内容。")
            return ToolObservation(
                display=_truncate(context),
                doc_ids=frozenset(r.chunk.document_id for r in results),
            )

        @tool
        async def get_document(document_id: int) -> ToolObservation:
            """获取指定知识库文档的完整内容。

            当 search_knowledge 只返回片段、需要看文档全文或结构时使用。参数为
            文档 id（可用 list_documents 查看文档 id 列表）。返回标题、状态和
            完整正文（超长文档会截断）。
            """
            async with AsyncSessionLocal() as s:
                doc = await s.get(Document, document_id)
                if doc is None:
                    return ToolObservation(
                        display=f"文档 {document_id} 不存在。可用 list_documents 查看文档列表。"
                    )
                body = doc.content or ""
                return ToolObservation(
                    display=_truncate(
                        f"标题: {doc.title}\n状态: {doc.status}\n"
                        f"来源: {doc.source_type}\n\n{body}"
                    ),
                    doc_ids=frozenset({document_id}),
                )

        @tool
        async def list_documents(limit: int = 20) -> str:
            """列出知识库中的文档（id + 标题 + 状态 + 分块数）。

            当需要了解知识库有哪些文档、或寻找特定文档 id 时使用。limit 为返回
            条数（默认 20，最多 50）。
            """
            limit = max(1, min(limit, 50))
            async with AsyncSessionLocal() as s:
                chunk_count_subq = (
                    select(func.count(KnowledgeChunk.id))
                    .where(KnowledgeChunk.document_id == Document.id)
                    .correlate(Document)
                    .scalar_subquery()
                )
                rows = (
                    await s.execute(
                        select(
                            Document.id, Document.title, Document.status,
                            chunk_count_subq.label("chunk_count"),
                        )
                        .where(Document.status == "ACTIVE")
                        .order_by(Document.id.desc())
                        .limit(limit)
                    )
                ).all()
            if not rows:
                return "知识库为空，暂无文档。"
            lines = ["知识库文档列表："]
            for r in rows:
                lines.append(f"- id={r.id} | {r.title} | 状态:{r.status} | 分块:{r.chunk_count}")
            return _truncate("\n".join(lines))

        @tool
        def calculate(expression: str) -> str:
            """计算数学表达式（支持 + - * / % **，含括号）。

            当需要精确算术计算时使用，避免凭空口算。expression 为算术表达式，
            如 '123 * 456 + 789'、'2024 % 4'。
            """
            try:
                result = _safe_eval(ast.parse(expression, mode="eval"))
            except (ValueError, SyntaxError, ZeroDivisionError) as exc:
                return f"计算失败: {exc}"
            return str(result)

        @tool
        def get_current_time() -> str:
            """获取当前日期和时间（含星期）。

            当用户问"现在几点""今天日期""星期几"时使用，避免模型臆测时间。
            """
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

        return [search_knowledge, get_document, list_documents, calculate, get_current_time]
