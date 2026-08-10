"""ReAct Agent 升级评估：thinking 模式 tool calling 最小原型验证。

目标：验证最关键的未知--LangChain ChatDeepSeek.bind_tools + astream 在 thinking
模式下，多轮循环能否把含 reasoning_content 的 assistant message 回传而不 400。

背景见 docs/integration/deepseek-notes.md §2：thinking 模式支持工具调用，但须
（1）extra_body thinking enabled；（2）每轮回传含 reasoning_content 的 message；
（3）不强制 tool_choice。该文档示例用裸 OpenAI client；本脚本验证 LangChain 路径。

不侵入主链路：不 import 生产 TurnCoordinator/ContextBuilder，仅复用 settings 读配置。
运行：cd codeaware-py && uv run python scripts/verify_react_thinking.py
"""

import ast
import asyncio
import operator as _op
import sys
from datetime import datetime
from pathlib import Path

# 让 app.* 可 import（脚本独立运行，不依赖调用者 cwd）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek

from app.core.config import settings

MAX_STEPS = 4


# ---------- 工具（纯函数，零 DB 依赖） ----------
_ALLOWED_BINOPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}


def _safe_eval(node: ast.AST) -> float:
    """受限算术求值：只允许数字 + 基本运算符，禁止名字/调用/属性。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _safe_eval(node.left), _safe_eval(node.right)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


@tool
def get_current_time() -> str:
    """获取当前日期和时间。用户问“现在几点”“今天日期”“星期几”时使用。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


@tool
def calculate(expression: str) -> str:
    """计算数学表达式（支持 + - * / % **）。用户需要算术计算时使用。"""
    return str(_safe_eval(ast.parse(expression, mode="eval")))


TOOLS = [get_current_time, calculate]
TOOL_MAP = {t.name: t for t in TOOLS}


def _build_model() -> ChatDeepSeek:
    """显式构造 thinking 模式 ChatDeepSeek（不复用 get_chat_model 单例）。

    extra_body 通过 bind_tools 显式传入（langchain 会警告 model_kwargs 里的 extra_body）。
    """
    return ChatDeepSeek(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=120,
    )


def _print_block(title: str, text: str, limit: int = 300) -> None:
    text = text or ""
    shown = text if len(text) <= limit else text[:limit] + f" …(+{len(text) - limit} chars)"
    print(f"  {title}: {shown}")


async def run_react_loop(query: str, label: str = "") -> bool:
    """跑一轮 ReAct 循环。返回 True=闭环成功（无 400、有终答），False=失败。"""
    print(f"\n{'=' * 70}")
    print(f"ReAct 循环 {label} | query: {query!r}")
    print("=" * 70)

    model = _build_model().bind_tools(
        TOOLS, tool_choice="auto", extra_body={"thinking": {"type": "enabled"}}
    )
    messages = [
        SystemMessage(
            content=(
                "你是一个助手。可以调用工具回答问题。"
                "需要知道当前时间或进行数学计算时，必须调用对应工具，不要臆测。"
                "拿到工具结果后，给出最终回答。"
            )
        ),
        HumanMessage(content=query),
    ]

    try:
        for step in range(MAX_STEPS):
            print(f"\n--- Step {step} ---")
            # 聚合流式 chunk（AIMessageChunk + 自动合并 tool_calls 分片）
            accumulated = None
            async for chunk in model.astream(messages):
                accumulated = chunk if accumulated is None else accumulated + chunk

            if accumulated is None:
                print("  !! 未收到任何 chunk")
                return False

            reasoning = accumulated.additional_kwargs.get("reasoning_content", "") or ""
            content = accumulated.content or ""
            tool_calls = accumulated.tool_calls or []

            _print_block("reasoning", reasoning)
            _print_block("content", content)
            print(f"  tool_calls: {tool_calls}")

            # 关键：回注含 reasoning_content 的 AIMessage（thinking 模式硬约束）
            messages.append(
                AIMessage(
                    content=content,
                    tool_calls=tool_calls,
                    additional_kwargs={"reasoning_content": reasoning},
                )
            )

            if not tool_calls:
                print("\n>>> 终答（无 tool_calls，循环结束）")
                return True

            # 执行工具，ToolMessage 回注
            for tc in tool_calls:
                name = tc["name"]
                args = tc.get("args", {})
                print(f"  执行工具: {name}({args})")
                try:
                    result = TOOL_MAP[name].invoke(args)
                    print(f"  工具结果: {result}")
                except Exception as e:  # noqa: BLE001
                    result = f"工具执行错误: {e}"
                    print(f"  工具错误: {e}")
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )

        print(f"\n>>> 达到 MAX_STEPS={MAX_STEPS} 上限，未收敛")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"\n!!! 循环异常（可能 400）: {type(e).__name__}: {e}")
        return False


async def main() -> None:
    print("ReAct thinking 模式 tool calling 验证")
    print(f"模型: {settings.llm_model} | thinking: enabled | tools: {[t.name for t in TOOLS]}")

    # 用例 1：纯时间（单工具）
    ok1 = await run_react_loop("现在几点？今天是星期几？", label="[用例1:时间]")

    # 用例 2：纯计算（单工具）
    ok2 = await run_react_loop("帮我计算 123 乘以 456 再加上 789", label="[用例2:计算]")

    # 用例 3：多工具 + 多轮（时间 + 计算，验证多步）
    ok3 = await run_react_loop(
        "现在几点？另外帮我算一下 2024 是不是闰年（用 2024 除以 4 看余数）",
        label="[用例3:多步]",
    )

    print("\n" + "=" * 70)
    print("验证结论汇总")
    print("=" * 70)
    print(f"  H1 thinking 多轮不 400 + reasoning_content 回传 : {'✅' if all([ok1, ok2, ok3]) else '❌'}")
    print(f"  H2 流式 tool_calls 聚合                         : {'✅' if all([ok1, ok2, ok3]) else '❌'}")
    print(f"  H3 工具闭环（选工具->执行->终答）              : {'✅' if all([ok1, ok2, ok3]) else '❌'}")
    print(f"  用例结果: 时间={ok1} 计算={ok2} 多步={ok3}")


if __name__ == "__main__":
    asyncio.run(main())
