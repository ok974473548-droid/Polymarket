"""Push-уведомления (этап 4): подписки устройств (Web Push).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "push_subscriptions" in have:  # повторный запуск не ломается
        return
    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.Text, primary_key=True),  # адрес службы push браузера (уникален на устройство)
        sa.Column("p256dh", sa.Text, nullable=False),  # открытый ключ устройства для шифрования
        sa.Column("auth", sa.Text, nullable=False),  # секрет устройства для шифрования
        sa.Column("label", sa.Text),  # браузер/устройство — для списка
        sa.Column("created_ts", sa.BigInteger, nullable=False),
        sa.Column("last_ok_ts", sa.BigInteger),
        sa.Column("fails", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("push_subscriptions")
