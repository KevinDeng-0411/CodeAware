"""ReAct 循环（ADR-0016）— Agent 模式的主循环。

模型自主决策（tool_choice=auto）：每轮 astream 后解析 tool_calls，执行工具、
回注 ToolMessage，直到无 tool_calls（终答）或达步数上限。复用原型已验证的模式：
- thinking 模式每轮回注含 reasoning_content 的 AIMessage（DeepSeek 硬约束，否则 400）
- 流式 tool_calls 用 AIMessageChunk 加法聚合（含并行工具）
- 防打转：seen 检测相同 (工具, 参数) 跳过重复执行
- 工具结果截断回注，避免撑爆上下文

本模块是 async generator：yield typed ChatEvent（reasoning/token/tool 事件），
最终答案填进 state.text（async generator 不能 return 值，用 state 传回）。

LLMOps（ADR-0017）：state 同时累积 trace（thought/tool_call/tool_result/answer 按序，
含每轮 reasoning 全文——持久化层按 agent_trace_include_reasoning 脱敏）、
stop_reason / tool_calls / error_tools，供 run 落库回放。
"""

from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.schemas.chat_events import ReasoningDelta, TokenDelta, ToolCall, ToolResult

DEFAULT_MAX_STEPS = 4
# observation 回注模型的最大字符数（工具结果截断，避免撑爆上下文）
MAX_TOOL_RESULT_CHARS = 2000
# per-tool 单轮调用上限（防过度调用：模型对信息不满足时反复取文档/检索发散，
# eval 实证 simple question 调 6 次 get_document 达步数上限。超限后该工具返回提示）
TOOL_CALL_LIMITS = {
    "search_knowledge": 3,
    "get_document": 2,
    "list_documents": 1,
    "calculate": 2,
    "get_current_time": 1,
}

# stop_reason 取值：final / no_output / max_steps / converged / error / cancelled
STOP_REASON_FINAL = "final"
STOP_REASON_NO_OUTPUT = "no_output"
STOP_REASON_MAX_STEPS = "max_steps"
STOP_REASON_CONVERGED = "converged"


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


def _trace_thought(state: ReactLoopState, step: int, reasoning: str) -> None:
    """追加 thought 条目。reasoning 全文暂存内存，持久化层按配置脱敏。"""
    state.trace.append(
        {"type": "thought", "step": step, "chars": len(reasoning), "reasoning": reasoning}
    )


def _trace_answer(state: ReactLoopState, step: int, content: str) -> None:
    state.trace.append({"type": "answer", "step": step, "content": content})


async def _execute_tool(tc: dict, tool_map: dict) -> tuple[str, bool, frozenset, bool]:
    """执行工具，返回 (展示文本, 是否成功, doc_ids 签名, 是否真实异常)。

    支持结构化 ToolObservation（search/get_document 返回 display + doc_ids）和
    纯 str（calculate/time 等无文档工具）。鸭子类型解构，避免循环依赖。

    第 4 项 is_exception 区分"工具真实异常"（需评审）与"正常返回错误文本"
    （未知工具/业务错误是合法结果）：ADR-0017 失败沉淀只追真实异常。
    """
    name = tc["name"]
    args = tc.get("args", {})
    if name not in tool_map:
        return f"未知工具: {name}（可用: {', '.join(tool_map)}）", False, frozenset(), False
    try:
        result = await tool_map[name].ainvoke(args)
        if hasattr(result, "display"):
            doc_ids = getattr(result, "doc_ids", frozenset())
            return str(result.display), True, frozenset(doc_ids), False
        return str(result), True, frozenset(), False
    except Exception as exc:  # noqa: BLE001
        return f"工具执行失败: {type(exc).__name__}: {exc}", False, frozenset(), True


async def react_loop(
    model,
    messages: list,
    tool_map: dict,
    cid: str,
    turn_id: str,
    nxt,
    state: ReactLoopState,
    max_steps: int = DEFAULT_MAX_STEPS,
):
    """ReAct 循环 async generator。

    model: bind_tools(tool_choice=auto, thinking enabled) 后的 ChatDeepSeek
    messages: 初始 LangChain messages（会被就地累积追加）
    tool_map: {工具名: BaseTool}
    nxt: sequence 生成器回调（单调递增）
    state: 结束时填充 text / steps / trace / stop_reason / tool_calls / error_tools
    """
    seen_calls: set[tuple[str, str]] = set()
    tool_counts: dict[str, int] = {}
    # 已观察到的文档 id（ADR-0016 收敛检测：检索/文档工具无新 doc_id → 强制终答）
    observed_docs: set[int] = set()
    for step in range(max_steps):
        round_no = step + 1
        # ---- 1. astream 聚合（含流式 yield reasoning/token）----
        accumulated = None
        async for chunk in model.astream(messages):
            if accumulated is None:
                accumulated = chunk
            else:
                accumulated = accumulated + chunk
            reasoning = (
                chunk.additional_kwargs.get("reasoning_content")
                if hasattr(chunk, "additional_kwargs")
                else None
            )
            if reasoning:
                yield ReasoningDelta(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=reasoning
                )
            delta = chunk.content if hasattr(chunk, "content") else str(chunk)
            if delta:
                yield TokenDelta(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=delta
                )

        if accumulated is None:
            # 无任何 chunk（异常已在调用方兜底，这里防御性退出）
            state.text = ""
            state.steps = round_no
            state.stop_reason = STOP_REASON_NO_OUTPUT
            return

        reasoning_full = (
            accumulated.additional_kwargs.get("reasoning_content", "")
            if hasattr(accumulated, "additional_kwargs")
            else ""
        )
        _trace_thought(state, round_no, reasoning_full)

        content = accumulated.content or ""
        tool_calls = accumulated.tool_calls or []

        # ---- 2. 回注含 reasoning_content 的 AIMessage（thinking 硬约束）----
        messages.append(
            AIMessage(
                content=content,
                tool_calls=tool_calls,
                additional_kwargs={
                    "reasoning_content": reasoning_full,
                },
            )
        )

        if not tool_calls:
            # ---- 3. 无工具调用：终答 ----
            state.text = content
            state.steps = round_no
            state.stop_reason = STOP_REASON_FINAL
            _trace_answer(state, round_no, content)
            return

        # ---- 4. 执行工具 ----
        round_doc_ids: set[int] = set()
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {})
            yield ToolCall(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                tool_name=name, tool_args=args, tool_call_id=tc["id"],
            )
            state.tool_calls += 1
            state.trace.append(
                {"type": "tool_call", "step": round_no, "name": name,
                 "args": args, "call_id": tc["id"]}
            )

            # 防过度调用：同工具调用次数超上限则不再执行（eval 实证模型会反复取文档发散）
            tool_counts[name] = tool_counts.get(name, 0) + 1
            limit = TOOL_CALL_LIMITS.get(name, 2)
            # 防打转：相同 (工具, 参数) 跳过重复执行
            seen_key = (name, str(sorted((args or {}).items())))
            if tool_counts[name] > limit:
                result, ok, doc_ids, is_exception = (
                    f"工具 {name} 已调用 {tool_counts[name]} 次（单轮上限 {limit}），"
                    "请基于已有信息直接回答，不要继续调用该工具。",
                    False,
                    frozenset(),
                    False,
                )
            elif seen_key in seen_calls:
                result, ok, doc_ids, is_exception = (
                    "该工具与参数组合已调用过，请基于已有结果回答或换一种问法。",
                    False,
                    frozenset(),
                    False,
                )
            else:
                seen_calls.add(seen_key)
                result, ok, doc_ids, is_exception = await _execute_tool(tc, tool_map)
                if is_exception:
                    state.error_tools += 1

            round_doc_ids |= set(doc_ids)

            result_text = result[:MAX_TOOL_RESULT_CHARS]
            yield ToolResult(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                tool_call_id=tc["id"], tool_name=name,
                status="ok" if ok else "error", result=result_text,
            )
            state.trace.append(
                {"type": "tool_result", "step": round_no, "call_id": tc["id"],
                 "status": "ok" if ok else "error", "result": result_text,
                 "doc_ids": sorted(doc_ids)}
            )
            messages.append(
                ToolMessage(content=result_text, tool_call_id=tc["id"])
            )

        # ---- 5. 检索收敛检测（ADR-0016 停止判断优化）----
        # 本轮有工具返回 doc_ids（检索/文档类工具），但全部已观察过 → 检索已收敛，
        # 再调只会重复取同一批文档 → 强制终答。模型调工具那轮 content 常为空，故注入
        # "基于已有信息回答"提示后再生成一轮（不再等模型自觉停 / per-tool 上限）。
        # round_doc_ids 非空即隐含调用了检索类工具（计算/时间等 doc_ids 恒空）。
        if round_doc_ids and round_doc_ids <= observed_docs:
            messages.append(
                HumanMessage(content="已获得足够信息。请直接基于以上工具结果给出最终回答，不要调用任何工具。")
            )
            accumulated = None
            async for chunk in model.astream(messages):
                if accumulated is None:
                    accumulated = chunk
                else:
                    accumulated = accumulated + chunk
                reasoning = (
                    chunk.additional_kwargs.get("reasoning_content")
                    if hasattr(chunk, "additional_kwargs")
                    else None
                )
                if reasoning:
                    yield ReasoningDelta(
                        conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=reasoning
                    )
                delta = chunk.content if hasattr(chunk, "content") else str(chunk)
                if delta:
                    yield TokenDelta(
                        conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=delta
                    )
            # 防御性：强制终答轮若仍产生 tool_calls（模型无视指令），标记而不执行
            if accumulated is not None and getattr(accumulated, "tool_calls", None):
                state.trace.append(
                    {"type": "convergence_override", "step": round_no,
                     "tool_calls": accumulated.tool_calls}
                )
            state.text = (accumulated.content or "") if accumulated else ""
            state.steps = round_no
            state.stop_reason = STOP_REASON_CONVERGED
            _trace_answer(state, round_no, state.text)
            return
        observed_docs |= round_doc_ids

    # ---- 达步数上限：用最后一轮内容兜底 ----
    state.text = accumulated.content or ""
    state.steps = max_steps
    state.stop_reason = STOP_REASON_MAX_STEPS
    _trace_answer(state, max_steps, state.text)
