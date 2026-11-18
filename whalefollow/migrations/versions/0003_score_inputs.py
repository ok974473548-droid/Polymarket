"""Инкрементальный пересчёт китов (этап 3): отпечаток входных данных каждого игрока на момент оценки.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "score_inputs" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "score_inputs",
        sa.Column("entity_id", sa.Text, primary_key=True),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("scored_at", sa.BigInteger),
    )


def downgrade() -> None:
    op.drop_table("score_inputs")
