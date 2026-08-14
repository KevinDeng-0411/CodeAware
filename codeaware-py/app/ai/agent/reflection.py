"""Reflection 节点（ADR-0018）：生成后自评，不达标注入 feedback 再生成。

轻量实现，默认关闭（settings.agent_reflection_enabled=False）。评估 draft 用
结构化输出（with_structured_output + function_calling，需**非 thinking** 模型，
thinking 下 function_calling 不可用，见 deepseek-notes.md），失败回退
ainvoke + 容错 JSON 解析。
"""

import json
import re

from pydantic import BaseModel, Field


class ReflectionVerdict(BaseModel):
    """自评结论：是否接受 + 不达标时的改进建议（中文）。"""

    accepted: bool
    feedback: str = Field(default="")


_REFLECT_PROMPT = """你是严格的答案质检员。评估下面的候选回答是否完整、准确、直接地回答了用户问题。

用户问题：
{question}

候选回答：
{draft}

判定标准：
- 完整：覆盖问题的关键信息，无遗漏；
- 准确：事实正确，无臆造；
- 直接：直接回答，不绕弯、不推诿。

仅当全部满足时才判 accepted=true。若 accepted=false，feedback 给出一句具体的中文改进建议。
只输出 JSON：{{"accepted": true/false, "feedback": "..."}}
"""


def _parse_verdict(content: str) -> ReflectionVerdict:
    """容错解析模型输出为 ReflectionVerdict（可能带 markdown 代码块/多余文本）。"""
    text = (content or "").strip()
    # 摘取首个 {...} 块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and "accepted" in data:
                return ReflectionVerdict(
                    accepted=bool(data.get("accepted")),
                    feedback=str(data.get("feedback", "")),
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # 解析失败默认接受（reflection 是增强，不能因解析失败阻塞终答）
    return ReflectionVerdict(accepted=True, feedback="")


async def evaluate_draft(model, question: str, draft: str) -> ReflectionVerdict:
    """评估 draft 是否合格。优先结构化输出，失败回退 ainvoke + 容错解析。"""
    prompt = _REFLECT_PROMPT.format(question=question, draft=draft)
    # 优先：with_structured_output（function_calling，非 thinking 模型上 schema 由函数签名强制）
    try:
        structured = model.with_structured_output(ReflectionVerdict, method="function_calling")
        result = await structured.ainvoke(prompt)
        if isinstance(result, ReflectionVerdict):
            return result
        if isinstance(result, dict):
            return ReflectionVerdict(**result)
        # 结构化输出返回了消息/文本内容：直接解析，避免再 ainvoke 二次调用
        content = getattr(result, "content", None) or str(result)
        return _parse_verdict(content)
    except Exception:  # noqa: BLE001
        pass
    # 回退：ainvoke + 解析 content
    try:
        resp = await model.ainvoke(prompt)
        content = getattr(resp, "content", None) or str(resp)
        if isinstance(resp, ReflectionVerdict):
            return resp
        return _parse_verdict(content)
    except Exception:  # noqa: BLE001
        return ReflectionVerdict(accepted=True, feedback="")
