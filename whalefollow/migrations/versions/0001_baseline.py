"""Стартовая схема: версия 0.13 (schema.sql + колонки, которые раньше дописывались при запуске).

Миграция повторяемая: на пустой базе создаёт всё, на старой базе (до Alembic) дописывает только
недостающие колонки и индексы, данные не трогает.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from __future__ import annotations

import re
from importlib import resources

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Колонки, добавленные после первых версий (бывший DB.MIGRATIONS) — часть стартовой схемы
LEGACY_COLUMNS = {
    "trades": {"source": "TEXT"},  # NULL — сделка; split | merge — операция из журнала кошелька
    "markets": {
        "fee_rate": "REAL",  # ставка комиссии тейкера: fee = shares × rate × p × (1−p)
        "url": "TEXT",
    },  # страница события на polymarket.com
    "signals": {"detected_ts": "INTEGER", "info": "TEXT"},
}


def _statements() -> list[str]:
    sql = resources.files("whalefollow").joinpath("schema.sql").read_text(encoding="utf-8")
    out = []
    for st in (s.strip() for s in sql.split(";")):
        body = "\n".join(ln for ln in st.splitlines() if not ln.strip().startswith("--")).strip()
        if body:
            out.append(body)
    return out


def upgrade() -> None:
    bind = op.get_bind()
    statements = _statements()
    tables = [b for b in statements if not b.upper().startswith("CREATE INDEX")]
    indexes = [b for b in statements if b.upper().startswith("CREATE INDEX")]
    if bind.dialect.name == "postgresql":
        # в SQLite INTEGER — 64 бита, в PostgreSQL — 32: суммы и отметки времени в мс не влезли бы
        tables = [re.sub(r"\bINTEGER\b", "BIGINT", b) for b in tables]
        # REAL в PostgreSQL — 32 бита (~7 знаков), в SQLite — 64: цены и суммы теряли бы точность
        tables = [re.sub(r"\bREAL\b", "DOUBLE PRECISION", b) for b in tables]
    for b in tables:
        bind.exec_driver_sql(b)
    insp = sa.inspect(bind)
    for table, cols in LEGACY_COLUMNS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for col, typ in cols.items():
            if bind.dialect.name == "postgresql":
                typ = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION"}.get(typ, typ)
            if col not in have:
                bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    for b in indexes:  # индексы — после колонок (могут ссылаться на новые)
        bind.exec_driver_sql(b)


def downgrade() -> None:
    raise NotImplementedError("Откат стартовой схемы не поддерживается: он удалил бы все данные")
