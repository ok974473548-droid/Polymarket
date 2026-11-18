"""Модуль «События», этап C: анализы событий — оценки модели по исходам на момент анализа.

Хранятся оценка каждого оценщика, итог модели, цена рынка в тот момент и неопределённость — по ним рисуется
линия модели на графике, а на этапе E проверяется точность «на тот момент» и подбираются веса.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "event_analyses" in have:
        return
    op.create_table(
        "event_analyses",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("ts", sa.BigInteger, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),  # ai | manual
        sa.Column("outcome_key", sa.Text, nullable=False),
        sa.Column("market_id", sa.Text),
        sa.Column("market_p", sa.Float),  # цена рынка в момент анализа
        sa.Column("anchor_p", sa.Float),  # якорь (цена или цена с поправкой)
        sa.Column("model_p", sa.Float),  # сводная независимая оценка (до сжатия к рынку)
        sa.Column("final_p", sa.Float),  # итог: якорь + λ·(модель − якорь)
        sa.Column("lo", sa.Float),
        sa.Column("hi", sa.Float),
        sa.Column("confidence", sa.Text),
        sa.Column("lam", sa.Float),
        sa.Column("dossier_ts", sa.BigInteger),
        sa.Column("components", sa.Text),  # JSON: [{name, p, w, q, note}]
    )
    op.create_index("ix_event_analyses_event", "event_analyses", ["event_id", "mode", "ts"])


def downgrade() -> None:
    op.drop_index("ix_event_analyses_event", "event_analyses")
    op.drop_table("event_analyses")
