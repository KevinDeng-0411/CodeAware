"""失败沉淀回归集（ADR-0017）。

线上 agent run 经评审 accepted 后，由 scripts/sync_regression_cases.py 把
(query, expected_tools, category) 元组追加进这里，test_agent_eval.py 的
AGENT_CASES 自动拼接，成为 eval 门禁 case。保持人工维护的 BASE 18 个不动。

条目格式与 AGENT_CASES 一致：(query: str, expected_tools: list[str], category: str)
"""

REGRESSION_CASES: list[tuple[str, list[str], str]] = []
