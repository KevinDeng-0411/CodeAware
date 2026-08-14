"""ADR-0018: Reflection 节点单元测试（图级，直接 build_agent_graph）。

覆盖：enabled 时 accepted（一次通过）/ rejected（feedback 注入再生成）/
达上限（接受最后一稿）；token 抑制（reflect 评估不产生 token 事件污染回答流）。
"""

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from app.ai.agent.agent_graph import build_agent_graph
from app.ai.agent.reflection import ReflectionVerdict


class _AgentModel:
    """agent 假模型：每次 astream 产出一稿 content（无工具），供 draft 生成。"""

    def __init__(self, contents):
        self.contents = contents
        self.astream_calls = 0

    async def astream(self, messages):
        content = self.contents[min(self.astream_calls, len(self.contents) - 1)]
        self.astream_calls += 1
        yield AIMessageChunk(content=content, additional_kwargs={"reasoning_content": ""})


class _ReflectModel:
    """reflect 假模型：with_structured_output 返回自身，ainvoke 按调用序返回判定。"""

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = 0

    def with_structured_output(self, schema, method=None):
        return self

    async def ainvoke(self, prompt):
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return v


def _make_init(question="q"):
    messages = [SystemMessage(content="sys"), HumanMessage(content=question)]
    return {
        "messages": messages, "steps": 0, "tool_counts": {}, "seen_calls": [],
        "observed_docs": [], "round_doc_ids": [], "trace": [],
        "stop_reason": "final", "tool_calls_total": 0, "error_tools": 0, "text": "",
        "converged_pending": False, "has_tool_calls": False, "converged_this_round": False,
        "tool_calls": [], "question": question, "reflections": 0, "reflection_done": False,
    }


async def _run_graph(graph, init):
    final = init
    events = []
    async for mode, chunk in graph.astream(init, stream_mode=["custom", "values"]):
        if mode == "custom":
            events.append(chunk)
        else:
            final = chunk
    return final, events


def _build(agent, reflector, max_reflections=1):
    return build_agent_graph(
        agent, {}, max_steps=4,
        reflection_model=reflector, reflection_enabled=True, max_reflections=max_reflections,
    )


async def test_reflection_accepted_single_draft():
    agent = _AgentModel(["直接回答"])
    reflector = _ReflectModel([ReflectionVerdict(accepted=True, feedback="")])
    final, _ = await _run_graph(_build(agent, reflector), _make_init())
    assert final["stop_reason"] == "final"
    assert final["text"] == "直接回答"
    assert final["reflections"] == 0
    assert agent.astream_calls == 1
    assert final["trace"][-1]["type"] == "answer"


async def test_reflection_rejected_then_regenerate():
    agent = _AgentModel(["太简略的草稿", "改进后的完整回答"])
    reflector = _ReflectModel([
        ReflectionVerdict(accepted=False, feedback="回答太简略"),
        ReflectionVerdict(accepted=True, feedback=""),
    ])
    final, _ = await _run_graph(_build(agent, reflector), _make_init())
    assert final["stop_reason"] == "final"
    assert final["text"] == "改进后的完整回答"
    assert final["reflections"] == 1
    assert agent.astream_calls == 2  # 一稿 + 一次再生成


async def test_reflection_max_reflections_accept_last():
    agent = _AgentModel(["草稿一", "草稿二"])
    reflector = _ReflectModel([ReflectionVerdict(accepted=False, feedback="还是不行")])
    final, _ = await _run_graph(_build(agent, reflector, max_reflections=1), _make_init())
    # 第一稿 reject（reflections 0<1）→ 再生成；第二稿 reject 但已达上限 → 接受第二稿
    assert final["stop_reason"] == "final"
    assert final["text"] == "草稿二"
    assert final["reflections"] == 1
    assert agent.astream_calls == 2


async def test_reflection_does_not_emit_tokens():
    """reflect 评估的模型调用不产生 token 事件（token 抑制）。"""
    agent = _AgentModel(["草稿", "改进"])
    reflector = _ReflectModel([
        ReflectionVerdict(accepted=False, feedback="不完整"),
        ReflectionVerdict(accepted=True, feedback=""),
    ])
    final, events = await _run_graph(_build(agent, reflector), _make_init())
    tokens = [e for e in events if e["type"] == "token"]
    # 只有 agent 两稿各自一个 token 事件；reflect 的 ainvoke 不 stream、不发 token
    assert len(tokens) == 2
    assert reflector.calls >= 1  # 评估确实发生了
    assert final["text"] == "改进"
