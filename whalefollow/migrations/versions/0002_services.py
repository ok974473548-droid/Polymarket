"""Разделение на сервисы (этап 2): живые события, состояние процессов, очередь проверок, живые цены.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _table(have: set[str], name: str, *cols: sa.Column, indexes: tuple = ()) -> None:
    if name in have:  # повторный запуск (например, после потери отметки версии) не ломается
        return
    op.create_table(name, *cols)
    for ix_name, ix_cols in indexes:
        op.create_index(ix_name, name, list(ix_cols))


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    # События для живого интерфейса между процессами: агент (realtime) пишет, поток SSE (api) читает
    _table(
        have,
        "live_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("data", sa.Text),
        indexes=(("ix_live_events_ts", ("ts",)),),
    )
    # Пульс процессов: жив ли каждый и когда последний раз работал (экран «Диагностика»)
    _table(
        have,
        "service_status",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("pid", sa.Integer),
        sa.Column("host", sa.Text),
        sa.Column("version", sa.Text),
        sa.Column("started_ts", sa.Float),
        sa.Column("last_ts", sa.Float),
        sa.Column("info", sa.Text),
    )
    # Проверки игроков: api ставит в очередь, worker выполняет, экран читает ход проверки
    _table(
        have,
        "check_jobs",
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),  # queued | running | done | error
        sa.Column("stage", sa.Text),
        sa.Column("done", sa.Integer),
        sa.Column("total", sa.Integer),
        sa.Column("started", sa.Float),
        sa.Column("finished", sa.Float),
        sa.Column("stages_done", sa.Text),
        sa.Column("card", sa.Text),
        sa.Column("result", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("timings", sa.Text),
        sa.Column("worker", sa.Text),
        sa.Column("updated_ts", sa.Float),
        indexes=(("ix_check_jobs_state", ("state", "started")), ("ix_check_jobs_address", ("address",))),
    )
    # Лучшие цены из потока стаканов (realtime) — для живых цен в интерфейсе (api)
    _table(
        have,
        "live_quotes",
        sa.Column("token_id", sa.Text, primary_key=True),
        sa.Column("bid", sa.Float),
        sa.Column("ask", sa.Float),
        sa.Column("ts", sa.Float),
    )


def downgrade() -> None:
    for t in ("live_quotes", "check_jobs", "service_status", "live_events"):
        op.drop_table(t)
