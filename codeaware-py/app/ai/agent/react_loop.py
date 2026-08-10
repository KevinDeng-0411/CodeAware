"""ReAct 循环（ADR-0016）— Agent 模式的主循环。

模型自主决策（tool_choice=auto）：每轮 astream 后解析 tool_calls，执行工具、
回注 ToolMessage，直到无 tool_calls（终答）或达步数上限。复用原型已验证的模式：
- thinking 模式每轮回注含 reasoning_content 的 AIMessage（DeepSeek 硬约束，否则 400）
- 流式 tool_calls 用 AIMessageChunk 加法聚合（含并行工具）
- 防打转：seen 检测相同 (工具, 参数) 跳过重复执行
- 工具结果截断回注，避免撑爆上下文

本模块是 async generator：yield typed ChatEvent（reasoning/token/tool 事件），
最终答案填进 state.text（async generator 不能 return 值，用 state 传回）。
"""

from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, ToolMessage

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


@dataclass
class ReactLoopState:
    """循环结果：最终答案 + 消耗步数。"""

    text: str = ""
    steps: int = 0


async def _execute_tool(tc: dict, tool_map: dict) -> tuple[str, bool]:
    """执行工具，返回 (结果字符串, 是否成功)。"""
    name = tc["name"]
    args = tc.get("args", {})
    if name not in tool_map:
        return f"未知工具: {name}（可用: {', '.join(tool_map)}）", False
    try:
        result = await tool_map[name].ainvoke(args)
        return str(result), True
    except Exception as exc:  # noqa: BLE001
        return f"工具执行失败: {type(exc).__name__}: {exc}", False


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
    state: 结束时填充 text / steps
    """
    seen_calls: set[tuple[str, str]] = set()
    tool_counts: dict[str, int] = {}
    for step in range(max_steps):
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
            state.steps = step + 1
            return

        content = accumulated.content or ""
        tool_calls = accumulated.tool_calls or []

        # ---- 2. 回注含 reasoning_content 的 AIMessage（thinking 硬约束）----
        messages.append(
            AIMessage(
                content=content,
                tool_calls=tool_calls,
                additional_kwargs={
                    "reasoning_content": accumulated.additional_kwargs.get(
                        "reasoning_content", ""
                    )
                },
            )
        )

        if not tool_calls:
            # ---- 3. 无工具调用：终答 ----
            state.text = content
            state.steps = step + 1
            return

        # ---- 4. 执行工具 ----
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {})
            yield ToolCall(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                tool_name=name, tool_args=args, tool_call_id=tc["id"],
            )

            # 防过度调用：同工具调用次数超上限则不再执行（eval 实证模型会反复取文档发散）
            tool_counts[name] = tool_counts.get(name, 0) + 1
            limit = TOOL_CALL_LIMITS.get(name, 2)
            # 防打转：相同 (工具, 参数) 跳过重复执行
            seen_key = (name, str(sorted((args or {}).items())))
            if tool_counts[name] > limit:
                result, ok = (
                    f"工具 {name} 已调用 {tool_counts[name]} 次（单轮上限 {limit}），"
                    "请基于已有信息直接回答，不要继续调用该工具。",
                    False,
                )
            elif seen_key in seen_calls:
                result, ok = "该工具与参数组合已调用过，请基于已有结果回答或换一种问法。", False
            else:
                seen_calls.add(seen_key)
                result, ok = await _execute_tool(tc, tool_map)

            result_text = result[:MAX_TOOL_RESULT_CHARS]
            yield ToolResult(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                tool_call_id=tc["id"], tool_name=name,
                status="ok" if ok else "error", result=result_text,
            )
            messages.append(
                ToolMessage(content=result_text, tool_call_id=tc["id"])
            )

    # ---- 达步数上限：用最后一轮内容兜底 ----
    state.text = accumulated.content or ""
    state.steps = max_steps
