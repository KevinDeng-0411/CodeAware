"""Guardrail - 请求边界注入检测（ADR-0017，最小实现）。

Agent 从用户查询取指令，可能覆盖 system prompt（prompt injection）。在
ChatRequest.message 的 Pydantic validator 处 fail-closed 拒绝可疑查询。

刻意不在工具结果层做注入检测（D2）：知识库是策展内容，对检索/文档内容做
模式匹配会误报（SQL/XSS 教学文档满屏关键字）且破坏模型理解。真实注入向量是
用户查询覆盖系统指令，故拦截点在请求边界。

纯函数、无副作用，便于单测。模式保守：要求"覆盖指令 + 系统上下文"同时出现，
宁可漏放也不误伤正常提问（如"什么是越狱"不命中）。
"""

import re

# 中英文注入模式（大小写不敏感）。触发即视为疑似提示注入。
INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?)"),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?)"),
    re.compile(r"you\s+are\s+now\s+(jailbroken|free\s+from\s+restrictions?)"),
    re.compile(r"reveal\s+(your|the)\s+(hidden\s+)?(system\s+)?prompts?"),
    re.compile(r"print\s+(your|the)\s+(hidden\s+)?(system\s+)?prompts?"),
    re.compile(r"忽略(以上|之前|前面)(所有)?(指令|提示|内容)"),
    re.compile(r"无视(以上|之前|前面)(所有)?(指令|提示|内容)"),
    re.compile(r"(泄露|透露|显示)(你的|系统)?(系统提示词|system prompt|指令)"),
)


def detect_query_injection(text: str) -> bool:
    """检测用户查询是否含提示注入模式。纯函数，无副作用。"""
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in INJECTION_PATTERNS)
