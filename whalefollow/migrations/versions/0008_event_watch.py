"""Модуль «События», этап D: «Следить» — события, по которым фоновая проверка присылает уведомление,
когда у модели появилась выгода.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "event_watch" in have:
        return
    op.create_table(
        "event_watch",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("mode", sa.Text),  # ai | manual | пусто — как в настройках
        sa.Column("checked_ts", sa.BigInteger),  # последняя фоновая проверка
        sa.Column("notified_ts", sa.BigInteger),  # последнее уведомление о выгоде
        sa.Column("state", sa.Text),  # JSON: выгодные стороны на момент проверки {ключ|сторона: выгода}
        sa.Column("last_error", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("event_watch")
