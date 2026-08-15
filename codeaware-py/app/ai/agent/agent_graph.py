"""LangGraph StateGraph 版 ReAct 编排（ADR-0018）。

手写 async generator 循环（原 react_loop.py）迁到 StateGraph：react_loop 变成薄壳，
对外签名 / SSE typed 事件契约不变。本模块是编排逻辑的唯一事实源。

关键语义（与原手写循环逐条对应，见 docs/roadmap/langgraph-react-migration.md）：
- agent 节点每轮聚合 model.astream（reasoning_content 回注是 thinking 硬约束）
- 无工具调用 → 终答；有 → tools 节点执行（防打转 seen_calls / per-tool 上限 TOOL_CALL_LIMITS）
- 检索收敛（round_doc_ids ⊆ observed_docs，对照原 `if round_doc_ids and round_doc_ids <= observed_docs`）
  → 注入 HumanMessage + converged_pending → 回 agent 强制终答轮（该轮不递增 steps）
- 事件不在此 yield：节点用 get_stream_writer() 发 custom 事件（reasoning/token/tool_call/tool_result），
  薄壳经 graph.astream(stream_mode=["custom","values"]) 转成 SSE typed 事件。
  （langgraph 1.2.10 的 custom 事件不走 astream_events 的 on_custom_event/on_chat_model_stream，
  且测试 FakeAgentLLM 非 runnable，故由节点主动发事件。）
"""

import json
import time
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.ai.agent.reflection import evaluate_draft
from app.core.config import settings

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

# 收敛强制终答轮注入的提示（模型调工具那轮 content 常为空，提示后再生成一轮）
_CONVERGENCE_HINT = "已获得足够信息。请直接基于以上工具结果给出最终回答，不要调用任何工具。"


class AgentState(TypedDict, total=False):
    """承载原 react_loop 局部状态 + 路由标记。

    messages/trace/seen_calls/tool_counts 是就地累积的共享可变对象（不随节点
    return 替换，LangGraph LastValue 通道保留同一引用）；其余为标量/每次重写。
    """

    messages: list
    steps: int
    tool_counts: dict
    seen_calls: list
    observed_docs: list
    round_doc_ids: list
    trace: list
    stop_reason: str
    tool_calls_total: int
    error_tools: int
    text: str
    converged_pending: bool
    has_tool_calls: bool
    converged_this_round: bool
    tool_calls: list
    question: str
    reflections: int
    reflection_done: bool
    draft_deltas: list


def _extract_usage(msg) -> dict | None:
    """从聚合后的 AIMessage/Chunk 归一化 token 用量：{input, output, reasoning} | None。

    实测 DeepSeek usage_metadata：{input_tokens, output_tokens, total_tokens,
    output_token_details:{reasoning}}。缺省返回 None（测试 fake 无 usage，不破坏现有断言）。
    """
    um = getattr(msg, "usage_metadata", None) if msg is not None else None
    if not um:
        return None
    details = um.get("output_token_details") or {}
    reasoning = details.get("reasoning") if isinstance(details, dict) else None
    return {
        "input": um.get("input_tokens") or 0,
        "output": um.get("output_tokens") or 0,
        "reasoning": reasoning or 0,
    }


def _trace_thought(trace: list, step: int, reasoning: str, tokens=None, ms=None) -> None:
    """追加 thought 条目。reasoning 全文暂存内存，持久化层按配置脱敏。"""
    entry = {"type": "thought", "step": step, "chars": len(reasoning), "reasoning": reasoning}
    if tokens:
        entry["tokens"] = tokens
    if ms is not None:
        entry["ms"] = ms
    trace.append(entry)


def _trace_answer(trace: list, step: int, content: str) -> None:
    trace.append({"type": "answer", "step": step, "content": content})


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


def build_agent_graph(
    model,
    tool_map: dict,
    max_steps: int = DEFAULT_MAX_STEPS,
    *,
    reflection_model=None,
    reflection_enabled: bool | None = None,
    max_reflections: int | None = None,
):
    """构建编译好的 StateGraph。

    model/tool_map/max_steps 以闭包捕获（每轮调用构建一次，代价可忽略）。
    节点必须是普通 async 函数（async generator 中间 yield 会被 LangGraph 丢弃），
    事件通过 get_stream_writer() 走 stream_mode="custom"。

    reflection_enabled / max_reflections 默认取 settings（agent_reflection_enabled /
    agent_max_reflections）；reflection_model 默认复用 model（生产因 thinking 绑定
    可能退化到 ainvoke 回退，见 ADR-0018）。
    """
    if reflection_model is None:
        reflection_model = model
    if reflection_enabled is None:
        reflection_enabled = settings.agent_reflection_enabled
    if max_reflections is None:
        max_reflections = settings.agent_max_reflections
    async def agent_node(state: AgentState) -> dict[str, Any]:
        """聚合模型流 + 回注 AIMessage；产出终答/工具调用的路由依据。

        reasoning/token 以 custom 事件经 get_stream_writer 发出（薄壳转 SSE）——
        langgraph 1.2.10 的 custom 事件走 graph.astream(stream_mode="custom")，
        不走 astream_events 的 on_chat_model_stream，故由节点主动发，且对测试的
        FakeAgentLLM（非 runnable）同样可用。
        """
        writer = get_stream_writer()
        messages = state["messages"]
        # 反射开启且非收敛强制轮：draft 内容缓冲，接受后才发（修 token 泄漏）；
        # 其余（反射关 / 收敛强制终答轮）照常实时流，行为不变
        converged_pending = bool(state.get("converged_pending"))
        stream_live = (not reflection_enabled) or converged_pending
        buf: list[str] = []
        round_start = time.perf_counter()
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
                writer({"type": "reasoning", "delta": reasoning})
            delta = chunk.content if hasattr(chunk, "content") else str(chunk)
            if delta:
                if stream_live:
                    writer({"type": "token", "delta": delta})
                else:
                    buf.append(delta)

        # 强制终答轮（convergence）不递增 steps：与原 `state.steps = round_no` 一致
        step = state.get("steps", 0)
        if not converged_pending:
            step += 1
        update: dict[str, Any] = {"steps": step}

        if accumulated is None:
            update.update(
                {"text": "", "stop_reason": STOP_REASON_NO_OUTPUT, "has_tool_calls": False}
            )
            return update

        reasoning_full = (
            accumulated.additional_kwargs.get("reasoning_content", "")
            if hasattr(accumulated, "additional_kwargs")
            else ""
        )
        trace = state["trace"]
        # 元数据扩展：每轮 token 用量（usage_metadata 聚合后保留）+ 调用耗时
        _trace_thought(
            trace, step, reasoning_full,
            tokens=_extract_usage(accumulated),
            ms=round((time.perf_counter() - round_start) * 1000),
        )

        content = accumulated.content or ""
        tool_calls = accumulated.tool_calls or []
        # thinking 硬约束：每轮回传含 reasoning_content 的 AIMessage（DeepSeek 否则 400）
        messages.append(
            AIMessage(
                content=content,
                tool_calls=tool_calls,
                additional_kwargs={"reasoning_content": reasoning_full},
            )
        )
        update["text"] = content  # 每轮记录，max_steps 兜底用最后一轮 content

        if converged_pending:
            # 强制终答轮仍出 tool_calls：标记 convergence_override，忽略工具直接终答
            if tool_calls:
                trace.append(
                    {"type": "convergence_override", "step": step, "tool_calls": tool_calls}
                )
            _trace_answer(trace, step, content)
            update.update(
                {"stop_reason": STOP_REASON_CONVERGED, "has_tool_calls": False,
                 "converged_pending": False}
            )
            return update

        if not tool_calls:
            # reflection 开启时：此处只产 draft，定稿/answer trace 交给 reflect 节点
            if not reflection_enabled:
                _trace_answer(trace, step, content)
                update["stop_reason"] = STOP_REASON_FINAL
            else:
                # draft：内容已缓冲，交给 reflect 接受后发（经 return 更新，LastValue replace）
                update["draft_deltas"] = buf
            update["has_tool_calls"] = False
            return update

        # 有 tool_calls：flush 缓冲的 content（反射关时 buf 空行为不变；工具轮 content 通常为空）
        for d in buf:
            writer({"type": "token", "delta": d})
        update.update({"has_tool_calls": True, "tool_calls": tool_calls})
        return update

    async def tools_node(state: AgentState) -> dict[str, Any]:
        """执行工具：发 custom 事件 + 防打转/per-tool 上限 + 收敛检测。"""
        messages = state["messages"]
        trace = state["trace"]
        tool_counts = state["tool_counts"]
        seen_calls = state["seen_calls"]
        observed_docs: set[int] = set(state["observed_docs"])
        step = state.get("steps", 1)
        writer = get_stream_writer()

        round_doc_ids: set[int] = set()
        tool_calls_total = state.get("tool_calls_total", 0)
        error_tools = state.get("error_tools", 0)

        for tc in state.get("tool_calls", []):
            name = tc["name"]
            args = tc.get("args", {})
            call_id = tc["id"]
            writer({"type": "tool_call", "name": name, "args": args, "call_id": call_id})
            tool_calls_total += 1
            tool_start = time.perf_counter()
            trace.append(
                {"type": "tool_call", "step": step, "name": name,
                 "args": args, "call_id": call_id}
            )

            tool_counts[name] = tool_counts.get(name, 0) + 1
            limit = TOOL_CALL_LIMITS.get(name, 2)
            # 防打转：相同 (工具, 参数) 跳过重复执行（json 化 key，序列化安全）
            seen_key = json.dumps([name, sorted((args or {}).items())], ensure_ascii=False)
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
                seen_calls.append(seen_key)
                result, ok, doc_ids, is_exception = await _execute_tool(tc, tool_map)
                if is_exception:
                    error_tools += 1

            round_doc_ids |= set(doc_ids)
            result_text = result[:MAX_TOOL_RESULT_CHARS]
            writer(
                {"type": "tool_result", "call_id": call_id, "name": name,
                 "status": "ok" if ok else "error", "result": result_text}
            )
            trace.append(
                {"type": "tool_result", "step": step, "call_id": call_id,
                 "status": "ok" if ok else "error", "result": result_text,
                 "doc_ids": sorted(doc_ids),
                 "ms": round((time.perf_counter() - tool_start) * 1000)}
            )
            messages.append(ToolMessage(content=result_text, tool_call_id=call_id))

        # 收敛检测用本轮前的 observed_docs（对照原：检查在 observed_docs |= 之前）
        converged = bool(round_doc_ids) and round_doc_ids <= observed_docs
        observed_docs |= round_doc_ids

        update: dict[str, Any] = {
            "observed_docs": sorted(observed_docs),
            "round_doc_ids": sorted(round_doc_ids),
            "tool_calls_total": tool_calls_total,
            "error_tools": error_tools,
            "converged_this_round": converged,
        }
        if converged:
            messages.append(HumanMessage(content=_CONVERGENCE_HINT))
            update["converged_pending"] = True
        elif step >= max_steps:
            update["stop_reason"] = STOP_REASON_MAX_STEPS
        return update

    async def reflect_node(state: AgentState) -> dict[str, Any]:
        """评估 draft：接受则定稿并发出缓冲的答案 token；拒绝且未达上限则注入 feedback 再生成。

        reflect 节点自身不产生 token 事件（结构化输出/ainvoke 的中间输出不进前端回答流，
        token 抑制）；发出的 draft token 是 agent 节点缓冲的 draft_deltas。
        """
        trace = state["trace"]
        step = state.get("steps", 1)
        draft = state.get("text", "")
        question = state.get("question", "")
        reflections = state.get("reflections", 0)
        reflect_start = time.perf_counter()
        verdict = await evaluate_draft(reflection_model, question, draft)

        # 记录反射判定（ADR-0017 观测：Agent Runs 可回放，前端流程视图渲染）。
        # feedback 是结构化输出，非 reasoning_content，不受 agent_trace_include_reasoning 脱敏影响
        trace.append(
            {
                "type": "reflection",
                "step": step,
                "attempt": reflections + 1,
                "accepted": verdict.accepted,
                "feedback": verdict.feedback,
                "ms": round((time.perf_counter() - reflect_start) * 1000),
            }
        )

        if verdict.accepted or reflections >= max_reflections:
            # 接受：把缓冲的 draft token 发出（前端只见最终被接受的答案，无泄漏）
            writer = get_stream_writer()
            for d in state.get("draft_deltas", []):
                writer({"type": "token", "delta": d})
            _trace_answer(trace, step, draft)
            return {"stop_reason": STOP_REASON_FINAL, "reflection_done": True, "draft_deltas": []}

        state["messages"].append(
            HumanMessage(
                content=(
                    f"上一轮回答未达要求：{verdict.feedback}\n"
                    "请根据反馈改进，直接给出最终答案，不要调用任何工具。"
                )
            )
        )
        return {"reflections": reflections + 1, "reflection_done": False, "draft_deltas": []}

    def _route_after_agent(state: AgentState) -> str:
        # 强制终答轮结束后（stop_reason=converged）或终答/无输出 → END
        if state.get("stop_reason") in (STOP_REASON_CONVERGED, STOP_REASON_NO_OUTPUT):
            return "end"
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return "reflect" if reflection_enabled else "end"

    def _route_after_tools(state: AgentState) -> str:
        if state.get("converged_this_round"):
            return "agent"  # 已注入 HumanMessage + converged_pending，回 agent 强制终答轮
        if state.get("steps", 0) >= max_steps:
            return "end"
        return "agent"

    def _route_after_reflect(state: AgentState) -> str:
        return "end" if state.get("reflection_done") else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    if reflection_enabled:
        graph.add_node("reflect", reflect_node)
        graph.add_conditional_edges(
            "agent", _route_after_agent,
            {"tools": "tools", "reflect": "reflect", "end": END},
        )
        graph.add_conditional_edges(
            "reflect", _route_after_reflect, {"agent": "agent", "end": END}
        )
    else:
        graph.add_conditional_edges(
            "agent", _route_after_agent, {"tools": "tools", "end": END}
        )
    graph.add_conditional_edges(
        "tools", _route_after_tools, {"agent": "agent", "end": END}
    )
    return graph.compile()
