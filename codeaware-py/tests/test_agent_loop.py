"""ADR-0016: ReAct 循环单元测试（纯逻辑，FakeAgentLLM 模拟 thinking 多轮）。

覆盖：工具调用->终答闭环、并行工具、未知工具降级、防打转去重、步数上限收敛、
reasoning_content 回注、ToolMessage 回注。
"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.ai.agent.react_loop import ReactLoopState, react_loop
from app.ai.agent.tools import ToolObservation


@tool
async def fake_calc(expression: str) -> str:
    """mock 计算工具。"""
    return f"fake:{expression}"


@tool
async def fake_get_doc(document_id: int) -> str:
    """mock 文档工具（无上限时应被限制）。"""
    return f"doc:{document_id}"


@tool
async def fake_search(query: str) -> ToolObservation:
    """mock 检索工具（固定返回 doc {1,2}，用于收敛检测）。"""
    return ToolObservation(display=f"结果:{query}", doc_ids=frozenset({1, 2}))


class FakeAgentLLM:
    """模拟 ChatDeepSeek.bind_tools + astream：按调用次数返回预设轮次。

    responses: list[dict]，每轮 {reasoning?, content?, tool_calls?:[{name,args,id}]}
    """

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.call_count = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages):
        r = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        tool_call_chunks = [
            {
                "name": tc["name"],
                "args": json.dumps(tc.get("args", {})),
                "id": tc.get("id", f"call_{i}"),
                "index": i,
            }
            for i, tc in enumerate(r.get("tool_calls", []))
        ]
        yield AIMessageChunk(
            content=r.get("content", ""),
            tool_call_chunks=tool_call_chunks,
            additional_kwargs={"reasoning_content": r.get("reasoning", "")},
        )


def _make_nxt():
    seq = {"v": 0}

    def nxt() -> int:
        seq["v"] += 1
        return seq["v"]

    return nxt


async def _run(model, responses_toolmap, max_steps=4):
    """跑一轮 react_loop，返回 (state, events, messages)。"""
    model = model
    messages = [SystemMessage(content="sys"), HumanMessage(content="q")]
    state = ReactLoopState()
    events = []
    async for ev in react_loop(
        model, messages, responses_toolmap, "cid", "tid", _make_nxt(), state, max_steps=max_steps
    ):
        events.append(ev)
    return state, events, messages


async def test_react_loop_tool_then_answer():
    """第一轮调工具 -> 执行 -> 第二轮终答。"""
    model = FakeAgentLLM([
        {"reasoning": "需要计算", "tool_calls": [{"name": "fake_calc", "args": {"expression": "1+1"}, "id": "call_1"}]},
        {"reasoning": "", "content": "结果是 2"},
    ])
    state, events, messages = await _run(model, {"fake_calc": fake_calc})
    assert state.text == "结果是 2"
    assert state.steps == 2
    kinds = {type(e).__name__ for e in events}
    assert {"ToolCall", "ToolResult", "TokenDelta"} <= kinds
    assert "ReasoningDelta" in kinds
    # reasoning_content 回注（thinking 硬约束）
    ai_msgs = [m for m in messages if isinstance(m, AIMessage)]
    assert ai_msgs and ai_msgs[0].additional_kwargs.get("reasoning_content") == "需要计算"
    # ToolMessage 回注 + 工具结果
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert tool_msgs and "fake:1+1" in tool_msgs[0].content


async def test_react_loop_parallel_tools():
    """并行工具调用：一轮多个 tool_calls，全部执行。"""
    model = FakeAgentLLM([
        {
            "reasoning": "",
            "tool_calls": [
                {"name": "fake_calc", "args": {"expression": "1+1"}, "id": "call_1"},
                {"name": "fake_calc", "args": {"expression": "2+2"}, "id": "call_2"},
            ],
        },
        {"reasoning": "", "content": "分别是 2 和 4"},
    ])
    state, events, messages = await _run(model, {"fake_calc": fake_calc})
    assert state.text == "分别是 2 和 4"
    tool_calls = [e for e in events if type(e).__name__ == "ToolCall"]
    assert len(tool_calls) == 2
    tool_results = [e for e in events if type(e).__name__ == "ToolResult"]
    assert len(tool_results) == 2


async def test_react_loop_unknown_tool_fallback():
    """模型调未知工具 -> 返回错误 observation，不崩溃。"""
    model = FakeAgentLLM([
        {"reasoning": "", "tool_calls": [{"name": "nonexistent_tool", "args": {}, "id": "call_1"}]},
        {"reasoning": "", "content": "我不认识这个工具"},
    ])
    state, events, messages = await _run(model, {"fake_calc": fake_calc})
    assert state.text == "我不认识这个工具"
    results = [e for e in events if type(e).__name__ == "ToolResult"]
    assert results and results[0].status == "error"
    assert "未知工具" in results[0].result


async def test_react_loop_seen_dedup():
    """防打转：相同 (工具, 参数) 第二轮跳过重复执行。"""
    model = FakeAgentLLM([
        {"reasoning": "", "tool_calls": [{"name": "fake_calc", "args": {"expression": "1+1"}, "id": "call_1"}]},
        {"reasoning": "", "tool_calls": [{"name": "fake_calc", "args": {"expression": "1+1"}, "id": "call_2"}]},
        {"reasoning": "", "content": "已算过"},
    ])
    state, events, messages = await _run(model, {"fake_calc": fake_calc})
    # 第二次相同调用被去重：ToolResult 为 error 且提示已调用过
    results = [e for e in events if type(e).__name__ == "ToolResult"]
    assert results[-1].status == "error"
    assert "已调用过" in results[-1].result
    assert state.text == "已算过"


async def test_react_loop_max_steps_bound():
    """步数上限：一直调工具 -> 达上限强制退出，用最后一轮 content 兜底。"""
    model = FakeAgentLLM([
        {"reasoning": "", "tool_calls": [{"name": "fake_calc", "args": {"expression": f"{i}"}, "id": f"call_{i}"}]}
        for i in range(10)
    ])
    state, events, messages = await _run(model, {"fake_calc": fake_calc}, max_steps=3)
    assert state.steps == 3
    # 未收敛，但状态机安全退出（无异常）
    assert isinstance(state.text, str)


async def test_react_loop_tool_call_limit():
    """防过度调用：同工具超过单轮上限后不再执行（eval 实证模型反复 get_document 发散）。"""
    model = FakeAgentLLM([
        # 连续 4 轮都调 fake_get_doc（不同 id），第 4 次应被上限拦截
        {"reasoning": "", "tool_calls": [{"name": "fake_get_doc", "args": {"document_id": i}, "id": f"call_{i}"}]}
        for i in range(4)
    ] + [{"reasoning": "", "content": "基于已有信息回答"}])
    state, events, messages = await _run(model, {"fake_get_doc": fake_get_doc}, max_steps=5)
    results = [e for e in events if type(e).__name__ == "ToolResult"]
    # get_document 上限 2 次：第 1、2 次执行（ok），第 3 次起被限制（error + 上限提示）
    ok_results = [r for r in results if r.status == "ok"]
    limited_results = [r for r in results if r.status == "error"]
    assert len(ok_results) == 2
    assert len(limited_results) >= 1
    assert "上限" in limited_results[0].result


async def test_react_loop_convergence_detection():
    """检索收敛检测：换 query 但返回相同 doc_ids（无新文档）→ 强制终答。

    用不同参数（query 缓存击穿/缓存穿透）避免 seen_calls 拦截，fake_search 固定返回
    {1,2}。第一轮 observed 空 → 累积；第二轮 {1,2} ⊆ observed → 收敛强制停。
    """
    model = FakeAgentLLM([
        {"reasoning": "", "tool_calls": [{"name": "fake_search", "args": {"query": "缓存击穿"}, "id": "call_1"}]},
        {"reasoning": "", "content": "基于检索结果回答", "tool_calls": [{"name": "fake_search", "args": {"query": "缓存穿透"}, "id": "call_2"}]},
    ])
    state, events, messages = await _run(model, {"fake_search": fake_search}, max_steps=5)
    # 收敛检测触发，提前停（steps=2 < max_steps=5），text 用第二轮 content
    assert state.steps == 2
    assert state.text == "基于检索结果回答"


async def test_react_loop_convergence_not_triggered_on_new_docs():
    """不同 doc_ids 不算收敛：每轮新文档 → 正常继续到终答。"""
    @tool
    async def fake_search_varying(query: str) -> ToolObservation:
        """按 query 长度返回不同 doc id，模拟'换 query 带来新文档'。"""
        return ToolObservation(display=f"结果:{query}", doc_ids=frozenset({len(query)}))

    model = FakeAgentLLM([
        {"reasoning": "", "tool_calls": [{"name": "fake_search_varying", "args": {"query": "ab"}, "id": "call_1"}]},
        {"reasoning": "", "tool_calls": [{"name": "fake_search_varying", "args": {"query": "abc"}, "id": "call_2"}]},
        {"reasoning": "", "content": "综合两个查询的结果回答"},
    ])
    state, events, messages = await _run(model, {"fake_search_varying": fake_search_varying}, max_steps=5)
    # 两轮各返回新 doc（ab→{2}, abc→{3}），不收敛，正常到第 3 轮终答
    assert state.steps == 3
    assert state.text == "综合两个查询的结果回答"
