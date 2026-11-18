"""Точность модели, F1: откуда анализ — страница события, «Следить» или теневой (фоновая выборка для самообучения).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("event_analyses")}
    if "origin" not in cols:
        op.add_column("event_analyses", sa.Column("origin", sa.Text))  # page | watch | shadow; старые — NULL (страница)


def downgrade() -> None:
    op.drop_column("event_analyses", "origin")
