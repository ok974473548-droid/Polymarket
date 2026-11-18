"""PostgreSQL: дробные числа — 64 бита (DOUBLE PRECISION), как в SQLite.

В базах PostgreSQL, созданных версиями 0.14–0.15, колонки REAL были 32-битными (~7 значащих цифр): цены, суммы
и оценки хранились с потерей точности. Миграция переводит их в DOUBLE PRECISION (уже потерянные знаки не
вернуть, новые значения хранятся точно). В SQLite ничего не меняется.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(
        sa.text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = 'real'"
        )
    ).fetchall()
    for table, column in rows:
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE DOUBLE PRECISION')


def downgrade() -> None:
    pass  # обратно к 32 битам не переводим: это только потеряло бы точность
