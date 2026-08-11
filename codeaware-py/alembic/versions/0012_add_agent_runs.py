"""add agent_runs table (LLMOps run trace, ADR-0017)

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column("stop_reason", sa.String(20), nullable=False, server_default="final"),
        sa.Column("steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_tools", sa.Integer, nullable=False, server_default="0"),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expected_tools", JSONB, nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("synced", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("trace", JSONB, nullable=False),
        sa.Column("context_snapshot", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_runs_turn_id", "agent_runs", ["turn_id"], unique=True)
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.create_index("ix_agent_runs_needs_review", "agent_runs", ["needs_review"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_needs_review", table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversation_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_turn_id", table_name="agent_runs")
    op.drop_table("agent_runs")
