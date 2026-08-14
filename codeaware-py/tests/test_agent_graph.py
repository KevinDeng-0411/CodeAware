"""ADR-0018: turn_coordinator agent 分支 SSE 流式测试（此前缺失）。

覆盖 agent 模式经 TurnCoordinator 全链路（prepare → run）的 SSE typed 事件契约：
ToolCall/ToolResult 顺序、sequence 严格递增、唯一终态（ChatCompleted）。
mock LLM 下发一轮 calculate 工具调用 + 一轮终答，验证 StateGraph 编排与薄壳转译
（react_loop 经 graph.astream 转 SSE 的路径）。
"""

import json

from langchain_core.messages import AIMessageChunk

from app.ai.agent.reflection import ReflectionVerdict
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.services.turn_coordinator import TurnCoordinator
from app.schemas.chat_events import ChatCompleted, ToolCall, ToolResult


class _AcceptReflection:
    """反射假模型：始终接受（不触发重写），SSE 契约测试确定。"""

    def with_structured_output(self, schema, method=None):
        return self

    async def ainvoke(self, prompt):
        return ReflectionVerdict(accepted=True, feedback="")


class _AgentLLM:
    """bind_tools + astream 的 agent 假模型：按轮次返回 tool_calls 或终答内容。

    astream 收 messages（list），逐轮按 call_count 取预设响应；ainvoke 供 post-turn
    摘要/抽取使用（返回固定内容）。
    """

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, prompt, **kwargs):
        class _R:
            content = "摘要"

        return _R()

    async def astream(self, messages):
        r = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        tcs = [
            {"name": t["name"], "args": json.dumps(t.get("args", {})),
             "id": t.get("id", f"c{i}"), "index": i}
            for i, t in enumerate(r.get("tool_calls", []))
        ]
        yield AIMessageChunk(
            content=r.get("content", ""),
            tool_call_chunks=tcs,
            additional_kwargs={"reasoning_content": r.get("reasoning", "")},
        )


def _build_coordinator(agent_llm, redis_client, vector_recall, chunker, mock_llm, lexical_recall):
    return TurnCoordinator(
        agent_llm, redis_client, vector_recall, chunker,
        QueryRewriter(mock_llm), lexical_recall,
        reflection_model=_AcceptReflection(),  # 反射默认开，注入 fake 避免打真实模型
    )


async def test_agent_branch_sse_stream_contract(
    db_session, redis_client, vector_recall, chunker, mock_llm, lexical_recall
):
    """agent 全链路：ToolCall 先于 ToolResult、sequence 单调、唯一 ChatCompleted 终态。"""
    agent_llm = _AgentLLM([
        {"reasoning": "需要计算", "tool_calls": [{"name": "calculate", "args": {"expression": "1+1"}, "id": "call_1"}]},
        {"reasoning": "", "content": "结果是 2"},
    ])
    coord = _build_coordinator(
        agent_llm, redis_client, vector_recall, chunker, mock_llm, lexical_recall
    )
    prepared = await coord.prepare_turn(None, "1+1等于几？", mode="agent")
    events = [ev async for ev in coord.run(prepared, "1+1等于几？")]

    # sequence 严格递增（SSE id==sequence，前端 fail-closed 校验）
    seqs = [ev.sequence for ev in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    # ToolCall 先于同 tool_call_id 的 ToolResult
    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    tool_results = {e.tool_call_id: e for e in events if isinstance(e, ToolResult)}
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc.tool_call_id in tool_results
    assert events.index(tc) < events.index(tool_results[tc.tool_call_id])
    assert tc.tool_name == "calculate"

    # 唯一终态（成功：ChatCompleted）
    completed = [e for e in events if isinstance(e, ChatCompleted)]
    assert len(completed) == 1
