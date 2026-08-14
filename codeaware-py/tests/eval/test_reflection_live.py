"""ADR-0018: Reflection 真实模型验证（live_eval）。

验证两块：
1. 非 thinking 模型 + function_calling 的判定能解析成 ReflectionVerdict（修"结构化
   输出是死代码"——真实 DeepSeek 上 function_calling 仅在非 thinking 可用）。
2. 全图 draft 缓冲无泄漏：只出一条答案 token 流，拼接 == 最终 text。

需要真实 DeepSeek key（settings.llm_api_key）。live_eval。
"""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.ai.agent.agent_graph import build_agent_graph
from app.ai.agent.reflection import ReflectionVerdict, evaluate_draft
from app.ai.config import get_chat_model, get_reflection_model

pytestmark = pytest.mark.live_eval


async def test_evaluate_draft_real_non_thinking_function_calling():
    """非 thinking 模型 + function_calling 判定能解析成 ReflectionVerdict。"""
    model = get_reflection_model()
    verdict = await evaluate_draft(model, "1+1 等于几？", "结果是 3。")
    assert isinstance(verdict, ReflectionVerdict)
    assert verdict.accepted in (True, False)


async def test_reflection_graph_no_draft_leak_real_model():
    """全图：draft 缓冲无泄漏——只出一条答案 token 流，拼接 == 最终 text。"""
    agent_model = get_chat_model()  # thinking（agent 正常生成）
    reflect_model = get_reflection_model()
    graph = build_agent_graph(
        agent_model, {}, max_steps=2,
        reflection_model=reflect_model, reflection_enabled=True, max_reflections=1,
    )
    messages = [
        SystemMessage(content="你是简洁的助手，直接回答，不要调用任何工具。"),
        HumanMessage(content="请用一句话回答：1+1 等于几？"),
    ]
    init = {
        "messages": messages, "steps": 0, "tool_counts": {}, "seen_calls": [],
        "observed_docs": [], "round_doc_ids": [], "trace": [],
        "stop_reason": "final", "tool_calls_total": 0, "error_tools": 0, "text": "",
        "converged_pending": False, "has_tool_calls": False, "converged_this_round": False,
        "tool_calls": [], "question": messages[-1].content, "reflections": 0,
        "reflection_done": False, "draft_deltas": [],
    }
    final = init
    tokens: list[str] = []
    async for mode, chunk in graph.astream(init, stream_mode=["custom", "values"]):
        if mode == "custom" and chunk["type"] == "token":
            tokens.append(chunk["delta"])
        elif mode == "values":
            final = chunk

    assert final["stop_reason"] == "final"
    assert final["draft_deltas"] == []
    # 无泄漏：前端看到的 token 拼接 == 落库的最终 text（草稿 token 不会混入）
    assert "".join(tokens) == final["text"]
