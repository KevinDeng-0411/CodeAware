"""失败沉淀同步（ADR-0017）：把评审 accepted 的线上 agent run 追加进 eval 回归集。

流程：Agent Runs 页评审 accepted（含 expected_tools + category）→ 本脚本把
review_status=accepted 且 synced=False 的 run 追加到 tests/eval/regression_cases.py
（幂等：按 (query, tools, category) 去重，保留已有条目）→ 置 synced=True。

用法（项目根 codeaware-py/，需 .env 连接开发库）：
    uv run python scripts/sync_regression_cases.py
"""

from __future__ import annotations

import asyncio
import ast
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

REGRESSION_FILE = APP_ROOT / "tests" / "eval" / "regression_cases.py"


def _load_existing() -> list[tuple[str, list[str], str]]:
    module = ast.parse(REGRESSION_FILE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REGRESSION_CASES" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            return [tuple(item) for item in value]
    return []


def _write_cases(cases: list[tuple[str, list[str], str]]) -> None:
    """保留文件头（docstring），只重写 REGRESSION_CASES 赋值。"""
    text = REGRESSION_FILE.read_text(encoding="utf-8")
    marker = "REGRESSION_CASES:"
    idx = text.index(marker)
    header = text[:idx]
    rendered = "\n".join(
        f"    {query!r}, {expected!r}, {category!r}," for query, expected, category in cases
    )
    REGRESSION_FILE.write_text(
        header + f"REGRESSION_CASES: list[tuple[str, list[str], str]] = [\n{rendered}\n]\n",
        encoding="utf-8",
    )


async def _main() -> int:
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models import AgentRun

    async with AsyncSessionLocal() as s:
        runs = (
            await s.execute(
                select(AgentRun)
                .where(AgentRun.review_status == "accepted", AgentRun.synced.is_(False))
                .order_by(AgentRun.id.asc())
            )
        ).scalars().all()

    if not runs:
        print("[sync] 无待同步的 accepted runs")
        return 0

    existing = _load_existing()
    # tools 是 list，不可哈希 → 用 (query, tuple(tools), category) 做去重键
    existing_ids = {(q, tuple(t), c) for q, t, c in existing}
    added = 0
    for run in runs:
        tools = run.expected_tools if isinstance(run.expected_tools, list) else []
        entry = (run.query, list(tools), run.category or "")
        key = (entry[0], tuple(entry[1]), entry[2])
        if key in existing_ids:
            continue
        existing.append(entry)
        existing_ids.add(key)
        added += 1

    if added:
        _write_cases(existing)
        print(f"[sync] 追加 {added} 条 → tests/eval/regression_cases.py（共 {len(existing)} 条）")
    else:
        print("[sync] 全部已存在（幂等，无新增）")

    # runs 来自已关闭的 session（detached），需按 id 重新查询才能持久化 synced 标记
    ids = [run.id for run in runs]
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AgentRun).where(AgentRun.id.in_(ids)))).scalars().all()
        for row in rows:
            row.synced = True
        await s.commit()
    print(f"[sync] 已标记 {len(runs)} 条 run 为 synced")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
