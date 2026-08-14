"""ADR-0018: Reflection 节点单元测试（图级，直接 build_agent_graph）。

覆盖：enabled 时 accepted（一次通过）/ rejected（feedback 注入再生成）/
达上限（接受最后一稿）；draft 缓冲（被拒草稿的 token 不泄漏——只有被接受的答案
发 token 事件，且 token 在 reasoning 之后）。
"""

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from app.ai.agent.agent_graph import build_agent_graph
from app.ai.agent.reflection import ReflectionVerdict


class _AgentModel:
    """agent 假模型：每次 astream 产出一稿（content + reasoning）。"""

    def __init__(self, contents):
        self.contents = contents
        self.astream_calls = 0

    async def astream(self, messages):
        content = self.contents[min(self.astream_calls, len(self.contents) - 1)]
        self.astream_calls += 1
        yield AIMessageChunk(
            content=content,
            additional_kwargs={"reasoning_content": f"思考{self.astream_calls}"},
        )


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
        "draft_deltas": [],
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
    final, events = await _run_graph(_build(agent, reflector), _make_init())
    assert final["stop_reason"] == "final"
    assert final["text"] == "直接回答"
    assert final["reflections"] == 0
    assert agent.astream_calls == 1
    assert final["trace"][-1]["type"] == "answer"
    # draft 缓冲：只有被接受的答案发 1 个 token，且在 reasoning 之后
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 1 and tokens[0]["delta"] == "直接回答"
    reasonings = [e for e in events if e["type"] == "reasoning"]
    assert len(reasonings) == 1
    assert events.index(reasonings[0]) < events.index(tokens[0])
    assert final["draft_deltas"] == []


async def test_reflection_rejected_then_regenerate():
    agent = _AgentModel(["太简略的草稿", "改进后的完整回答"])
    reflector = _ReflectModel([
        ReflectionVerdict(accepted=False, feedback="回答太简略"),
        ReflectionVerdict(accepted=True, feedback=""),
    ])
    final, events = await _run_graph(_build(agent, reflector), _make_init())
    assert final["stop_reason"] == "final"
    assert final["text"] == "改进后的完整回答"
    assert final["reflections"] == 1
    assert agent.astream_calls == 2  # 一稿 + 一次再生成
    # 被拒草稿的 token 不泄漏：只有最终接受的答案发 1 个 token
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 1 and tokens[0]["delta"] == "改进后的完整回答"
    assert final["draft_deltas"] == []


async def test_reflection_max_reflections_accept_last():
    agent = _AgentModel(["草稿一", "草稿二"])
    reflector = _ReflectModel([ReflectionVerdict(accepted=False, feedback="还是不行")])
    final, events = await _run_graph(_build(agent, reflector, max_reflections=1), _make_init())
    # 第一稿 reject（reflections 0<1）→ 再生成；第二稿 reject 但已达上限 → 接受第二稿
    assert final["stop_reason"] == "final"
    assert final["text"] == "草稿二"
    assert final["reflections"] == 1
    assert agent.astream_calls == 2
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 1 and tokens[0]["delta"] == "草稿二"
    assert final["draft_deltas"] == []


async def test_reflection_draft_not_leaked_single_answer_token():
    """draft 缓冲：被拒草稿的 token 不泄漏，只有被接受的答案发 1 个 token 事件。"""
    agent = _AgentModel(["草稿", "改进"])
    reflector = _ReflectModel([
        ReflectionVerdict(accepted=False, feedback="不完整"),
        ReflectionVerdict(accepted=True, feedback=""),
    ])
    final, events = await _run_graph(_build(agent, reflector), _make_init())
    tokens = [e for e in events if e["type"] == "token"]
    reasonings = [e for e in events if e["type"] == "reasoning"]
    # 只有被接受的"改进"发 token；"草稿"的 token 被缓冲丢弃（无泄漏）
    assert len(tokens) == 1
    assert tokens[0]["delta"] == "改进"
    # draft 轮 reasoning 实时流（2 稿各 1 条），token 在 reasoning 之后
    assert len(reasonings) == 2
    assert events.index(reasonings[-1]) < events.index(tokens[0])
    assert final["draft_deltas"] == []
    assert reflector.calls >= 1  # 评估确实发生了
