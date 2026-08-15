"""add agent_runs.usage (run-level token/ms/cost metadata)

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("usage", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "usage")
