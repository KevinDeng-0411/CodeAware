"""Agent 模式评估（ADR-0016）——工具决策准确率 + ReAct 闭环率。

衡量模型在 ReAct 循环中的**工具决策质量**（该调哪个工具、要不要调、能否闭环），
复用生产代码：AgentToolkit + react_loop + _build_agent_system_prompt。

需要真实 DeepSeek（工具决策 judge）+ Ollama bge-m3（embedding）+ BM25 索引。live_eval。
"""

import json
import os

import pytest

from app.ai.agent.react_loop import ReactLoopState, react_loop
from app.ai.agent.tools import AgentToolkit
from app.ai.config import get_chat_model, get_embedding_model
from app.ai.infra.vector_recall import VectorRecallService
from app.ai.rag.lexical_recall import Bm25LexicalRecall
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.rag.semantic_chunker import SemanticChunker
from app.db.session import AsyncSessionLocal, engine
from app.models import Document, KnowledgeChunk
from tests.eval.golden_retrieval import FIXTURE_DOCS

pytestmark = pytest.mark.live_eval

OUTPUT_FILE = "tests/eval/artifacts/agent_eval.json"
MAX_STEPS = 4

# 期望工具序列判据：模型自主决策，允许一定灵活性，用"该调的都调了（recall）"为主指标，
# 精确匹配为参考。direct（常识）要求不调任何工具（precision=1）。
AGENT_CASES = [
    # (query, expected_tools, category) — 每类 1 个核心 case（控制 live eval 耗时）
    ("缓存击穿怎么解决？", ["search_knowledge"], "need_search"),
    ("缓存击穿方案的完整内容是什么？", ["search_knowledge", "get_document"], "need_doc"),
    ("帮我计算 123 乘以 456", ["calculate"], "need_calc"),
    ("现在几点？", ["get_current_time"], "need_time"),
    ("对比缓存击穿和缓存穿透的解决方案", ["search_knowledge", "search_knowledge"], "multi_step"),
    ("你好", [], "direct"),
]


def _seq() -> callable:
    seq = {"v": 0}

    def nxt() -> int:
        seq["v"] += 1
        return seq["v"]

    return nxt


async def test_agent_tool_decision_and_closure(setup_db):
    # ---- 1. 建索引 + 上传 fixture docs（复用 rag_graph_eval 的 setup 模式）----
    embedder = get_embedding_model()
    vr = VectorRecallService(embedder)
    chunker = SemanticChunker()
    from app.ai.rag.chinese_segmenter import segment_chinese

    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_search"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_kc_chunk_content_segmented_bm25 "
            "ON knowledge_chunks USING bm25 (chunk_content_segmented) "
            "WITH (key_field='id', "
            "text_fields='{\"chunk_content_segmented\": "
            "{\"tokenizer\": {\"type\": \"default\"}}}')"
        ))
    async with AsyncSessionLocal() as s:
        doc = Document(title="_init", content="_init", source_type="MANUAL")
        s.add(doc)
        await s.flush()
        s.add(KnowledgeChunk(
            document_id=doc.id, chunk_index=0,
            chunk_content="_init", chunk_content_segmented="_init",
        ))
        await s.commit()
        for fd in FIXTURE_DOCS:
            d = Document(title=fd.title, content=fd.content, source_type="MANUAL")
            s.add(d)
            await s.flush()
            for ct in chunker.chunk(fd.content, content_type="md"):
                s.add(KnowledgeChunk(
                    document_id=d.id, chunk_index=0,
                    chunk_content=ct, chunk_content_segmented=segment_chinese(ct),
                    embedding=await embedder.aembed_query(ct),
                ))
        await s.commit()

    # ---- 2. Agent 工具 + 模型 ----
    llm = get_chat_model()
    toolkit = AgentToolkit(vr, Bm25LexicalRecall(), QueryRewriter(llm), chunker)
    tools = toolkit.get_tools()
    tool_map = {t.name: t for t in tools}
    tools_desc = "\n".join(f"- {t.name}: {t.description}" for t in tools)
    from app.ai.services.turn_coordinator import TurnCoordinator

    system_prompt = TurnCoordinator._build_agent_system_prompt(tools_desc)
    bound = llm.bind_tools(
        tools, tool_choice="auto", extra_body={"thinking": {"type": "enabled"}}
    )

    # ---- 3. 逐 case 跑 ReAct 循环 ----
    from langchain_core.messages import HumanMessage, SystemMessage

    rows = []
    for query, expected, category in AGENT_CASES:
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=query)]
        state = ReactLoopState()
        called: list[str] = []
        try:
            async for ev in react_loop(
                bound, messages, tool_map, "eval_cid", "eval_tid", _seq(), state, max_steps=MAX_STEPS
            ):
                from app.schemas.chat_events import ToolCall

                if isinstance(ev, ToolCall):
                    called.append(ev.tool_name)
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "query": query, "category": category,
                "expected": expected, "predicted": called,
                "error": f"{type(exc).__name__}: {exc}",
                "closed": False, "steps": state.steps, "answer_preview": "",
            })
            continue

        expected_set = set(expected)
        predicted_set = set(called)
        rows.append({
            "query": query, "category": category,
            "expected": expected, "predicted": called,
            "error": None,
            # 主指标：期望工具是否都被调用（该调的都调）
            "recall": expected_set.issubset(predicted_set) if expected_set else (predicted_set == set()),
            # 精确：调用集 == 期望集
            "exact": predicted_set == expected_set,
            "closed": bool(state.text.strip()),
            "steps": state.steps,
            "answer_preview": state.text[:120],
        })

    # ---- 4. 统计 ----
    n = len(rows)
    recall_ok = sum(1 for r in rows if r["recall"])
    exact_ok = sum(1 for r in rows if r["exact"])
    closed_ok = sum(1 for r in rows if r["closed"])
    avg_steps = sum(r["steps"] for r in rows) / n
    direct_rows = [r for r in rows if r["category"] == "direct"]
    direct_no_tool = sum(1 for r in direct_rows if not r["predicted"])

    result = {
        "n": n,
        "tool_selection": {
            "recall_mean": round(recall_ok / n, 3),
            "exact_mean": round(exact_ok / n, 3),
            "recall_ok": recall_ok,
        },
        "closure": {
            "closed_mean": round(closed_ok / n, 3),
            "closed_ok": closed_ok,
            "avg_steps": round(avg_steps, 2),
        },
        "direct": {
            "n": len(direct_rows),
            "no_tool": direct_no_tool,
            "no_tool_rate": round(direct_no_tool / len(direct_rows), 3) if direct_rows else 1.0,
        },
        "rows": rows,
    }
    os.makedirs("tests/eval/artifacts", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n[AGENT-EVAL] saved to {OUTPUT_FILE}")
    print(f"[AGENT-EVAL] tool recall = {result['tool_selection']['recall_mean']:.3f} "
          f"({recall_ok}/{n})")
    print(f"[AGENT-EVAL] closure = {result['closure']['closed_mean']:.3f} ({closed_ok}/{n}), "
          f"avg steps = {avg_steps:.2f}")
    print(f"[AGENT-EVAL] direct no-tool = {result['direct']['no_tool_rate']:.3f} "
          f"({direct_no_tool}/{len(direct_rows)})")

    # 门禁：该调的都调（recall）>= 0.7；闭环率 >= 0.9；direct 不误调工具 = 100%
    assert recall_ok / n >= 0.7, f"tool recall {recall_ok / n:.3f} < 0.7"
    assert closed_ok / n >= 0.9, f"closure {closed_ok / n:.3f} < 0.9"
    assert direct_no_tool == len(direct_rows), f"direct cases called tools: {direct_rows}"
