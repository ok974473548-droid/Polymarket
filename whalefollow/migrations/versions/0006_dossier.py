"""Модуль «События», этап B: досье события — собранные факты по событию и итог каждого сбора.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "event_dossiers" not in have:
        op.create_table(
            "event_dossiers",  # последний сбор досье по событию
            sa.Column("event_id", sa.Text, primary_key=True),
            sa.Column("title", sa.Text),
            sa.Column("topic", sa.Text),
            sa.Column("collected_ts", sa.BigInteger),
            sa.Column("n_items", sa.Integer),
            sa.Column("sources", sa.Text),  # JSON: источник → {"n", "error", "ms"}
        )
    if "evidence" not in have:
        op.create_table(
            "evidence",  # факты досье; хранятся с моментом сбора (для проверки модели «на момент» на этапе E)
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("event_id", sa.Text, nullable=False),
            sa.Column("collected_ts", sa.BigInteger, nullable=False),
            sa.Column("kind", sa.Text, nullable=False),  # news | forecast | data | flow
            sa.Column("source", sa.Text),
            sa.Column("title", sa.Text),
            sa.Column("url", sa.Text),
            sa.Column("ts_pub", sa.BigInteger),
            sa.Column("value", sa.Float),
            sa.Column("unit", sa.Text),
            sa.Column("outcome_key", sa.Text),
            sa.Column("note", sa.Text),
            sa.Column("weight", sa.Float),
            sa.Column("meta", sa.Text),
        )
        op.create_index("ix_evidence_event", "evidence", ["event_id", "collected_ts"])


def downgrade() -> None:
    op.drop_index("ix_evidence_event", "evidence")
    op.drop_table("evidence")
    op.drop_table("event_dossiers")
