"""ReAct 循环薄壳（ADR-0016/0018）— Agent 模式的主循环对外入口。

编排逻辑已迁到 LangGraph StateGraph（app.ai.agent.agent_graph，ADR-0018）。
本模块保留对外契约不变（签名 / yield 的 typed ChatEvent / 结束时填 ReactLoopState），
内部跑真 StateGraph：
- 每轮回注含 reasoning_content 的 AIMessage（DeepSeek thinking 硬约束）
- 流式 tool_calls 用 AIMessageChunk 加法聚合（含并行工具）
- 防打转：seen 检测相同 (工具, 参数) 跳过重复执行
- 工具结果截断回注，避免撑爆上下文
- 检索收敛检测（ADR-0016 停止判断）：无新 doc_id → 强制终答

本模块是 async generator：yield typed ChatEvent（reasoning/token/tool 事件），
最终答案填进 state.text（async generator 不能 return 值，用 state 传回）。

LLMOps（ADR-0017）：state 同时累积 trace（thought/tool_call/tool_result/answer 按序，
含每轮 reasoning 全文——持久化层按 agent_trace_include_reasoning 脱敏）、
stop_reason / tool_calls / error_tools，供 run 落库回放。
"""

from dataclasses import dataclass, field

from langgraph.graph.state import CompiledStateGraph

from app.ai.agent.agent_graph import (
    DEFAULT_MAX_STEPS,
    STOP_REASON_FINAL,
    build_agent_graph,
)
from app.schemas.chat_events import ReasoningDelta, TokenDelta, ToolCall, ToolResult


@dataclass
class ReactLoopState:
    """循环结果：最终答案 + 消耗步数 + trace（ADR-0017）。

    trace/stop_reason/tool_calls/error_tools 为 LLMOps 观测字段（带默认值，
    向后兼容 eval 直接构造 ReactLoopState()）。
    """

    text: str = ""
    steps: int = 0
    trace: list = field(default_factory=list)
    stop_reason: str = STOP_REASON_FINAL
    tool_calls: int = 0
    error_tools: int = 0


async def react_loop(
    model,
    messages: list,
    tool_map: dict,
    cid: str,
    turn_id: str,
    nxt,
    state: ReactLoopState,
    max_steps: int = DEFAULT_MAX_STEPS,
    *,
    reflection_model=None,
):
    """ReAct 循环 async generator（薄壳，内部跑 StateGraph）。

    model: bind_tools(tool_choice=auto, thinking enabled) 后的 ChatDeepSeek
    messages: 初始 LangChain messages（会被就地累积追加）
    tool_map: {工具名: BaseTool}
    nxt: sequence 生成器回调（单调递增；yield 时现分配，保证 SSE id 严格递增）
    state: 结束时填充 text / steps / trace / stop_reason / tool_calls / error_tools
    reflection_model: Reflection 评估用模型（应传非 thinking 实例，见 get_chat_model(thinking=False)）；
        缺省时复用 model（bind_tools 绑定模型上 function_calling 不可用，仅测试用）
    """
    graph: CompiledStateGraph = build_agent_graph(
        model, tool_map, max_steps, reflection_model=reflection_model
    )
    init = {
        "messages": messages,
        "steps": 0,
        "tool_counts": {},
        "seen_calls": [],
        "observed_docs": [],
        "round_doc_ids": [],
        "trace": state.trace,
        "stop_reason": STOP_REASON_FINAL,
        "tool_calls_total": 0,
        "error_tools": 0,
        "text": "",
        "converged_pending": False,
        "has_tool_calls": False,
        "converged_this_round": False,
        "tool_calls": [],
        "question": messages[-1].content if messages else "",
        "reflections": 0,
        "reflection_done": False,
        "draft_deltas": [],
    }
    final = init
    async for mode, chunk in graph.astream(
        init, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            payload = chunk
            kind = payload["type"]
            if kind == "reasoning":
                yield ReasoningDelta(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=payload["delta"]
                )
            elif kind == "token":
                yield TokenDelta(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=payload["delta"]
                )
            elif kind == "tool_call":
                yield ToolCall(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                    tool_name=payload["name"], tool_args=payload["args"],
                    tool_call_id=payload["call_id"],
                )
            elif kind == "tool_result":
                yield ToolResult(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                    tool_call_id=payload["call_id"], tool_name=payload["name"],
                    status=payload["status"], result=payload["result"],
                )
        else:  # "values"：每个 superstep 后的完整状态，最后一个是终态
            final = chunk

    # 终态回填 state（单次流，不重复跑图）
    state.text = final.get("text", "")
    state.steps = final.get("steps", 0)
    state.trace = final.get("trace", state.trace)
    state.stop_reason = final.get("stop_reason", STOP_REASON_FINAL)
    state.tool_calls = final.get("tool_calls_total", 0)
    state.error_tools = final.get("error_tools", 0)
