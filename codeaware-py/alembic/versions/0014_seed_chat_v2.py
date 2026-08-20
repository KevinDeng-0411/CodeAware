"""seed CHAT prompt template v2（上下文优先级 + 记忆仲裁 + 回答纪律 + 示例）

Revision ID: 0014
Revises: 0013

v1 只有一句规则（"超出范围就用专业知识回答"），无来源优先级、无记忆仲裁、无引用纪律，
且"超出就用专业知识"是幻觉诱导器。v2 按 5 原则重写：
- 上下文来源优先级：知识库文档 > 长期记忆 > 对话历史（冲突时低优先级只作补充）
- 长期记忆仲裁：记忆是"线索"而非"结论"，与知识库冲突以知识库为准（与记忆改进 A 层对齐）
- 回答纪律：引用出处、区分「知识库依据」vs「个人推测」、禁止编造
- few-shot 示例：只演示两个难点——冲突仲裁 + 知识库信息不足的有界回退

激活语义（ADR-0005）：每 type 恰一 active（partial unique index）。迁移内先 deactivate
旧 active 再 insert v2，保持约束不违反。v1 保留留档，可经 Prompt API activate(id) 回滚。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHAT_ROLE_SETTING_V2 = (
    "你是 CodeAware 助手，服务于一个软件开发团队。你的职责是基于给定上下文准确、诚实地回答用户问题。"
)

CHAT_PROMPT_BODY_V2 = """## 上下文使用优先级
回答时按下述优先级使用上下文。低优先级来源只作补充，不得覆盖高优先级信息：
1. 知识库文档 —— 唯一可信的事实来源
2. 长期记忆 —— 关于团队/项目的偏好与已发生事实，仅作线索
3. 对话历史 —— 用于理解上下文与追问，不构成事实

## 长期记忆仲裁
- 长期记忆是"提示"而非"结论"：可能过时、冲突或与知识库不一致。
- 当长期记忆与知识库冲突时，以知识库文档为准，并简要说明差异。
- 记忆未覆盖的细节，不得臆造填补。

## 回答纪律
- 优先引用知识库文档回答，并尽量指出出处（文档/章节）。
- 知识库信息不足时，诚实说明"知识库中没有足够信息"；可基于专业知识给出一般性建议，但必须明确区分「知识库依据」与「个人推测」。
- 禁止编造文档内容、引用、数字或来源。

## 示例
【示例 1 · 记忆与知识库冲突】
知识库文档：《deploy.md》写明 "当前生产环境使用 SSH + 密钥认证部署"。
长期记忆：部署已改用 HTTP 明文。
问题：团队现在怎么部署？
正确回答：根据《deploy.md》，团队当前使用 SSH + 密钥认证部署。长期记忆提到"改用 HTTP 明文"，与文档不一致，以知识库为准。

【示例 2 · 知识库信息不足】
知识库文档：（无相关文档）
问题：如何配置我们的 CI 缓存？
正确回答：知识库中没有关于 CI 缓存配置的文档。基于一般实践，建议先检查仓库中的 CI 配置；如需精确方案，请先补充相关文档到知识库。

## 长期记忆
{{long_term_memory}}

## 相关知识库文档
{{rag_context}}

## 对话历史
{{conversation_history}}

## 用户问题
{{user_message}}"""


def upgrade() -> None:
    bind = op.get_bind()
    # ADR-0005：先 deactivate 当前 active（v1），避免与 v2 同时 active 违反部分唯一索引
    bind.execute(
        sa.text(
            "UPDATE prompt_templates SET is_active = false "
            "WHERE type = 'CHAT' AND is_active = true"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO prompt_templates "
            "(type, version, name, role_setting, template_body, review_dimensions, severity_levels, is_active) "
            "VALUES (:type, :version, :name, :role_setting, :template_body, :review_dimensions, :severity_levels, :is_active)"
        ),
        {
            "type": "CHAT",
            "version": 2,
            "name": "Chat 系统提示词 v2",
            "role_setting": CHAT_ROLE_SETTING_V2,
            "review_dimensions": None,
            "severity_levels": None,
            "is_active": True,
            "template_body": CHAT_PROMPT_BODY_V2,
        },
    )


def downgrade() -> None:
    """回滚：删 v2，恢复 v1 active（保持每 type 恰一 active）。"""
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM prompt_templates WHERE type = 'CHAT' AND version = 2"))
    bind.execute(
        sa.text(
            "UPDATE prompt_templates SET is_active = true "
            "WHERE type = 'CHAT' AND version = 1"
        )
    )
