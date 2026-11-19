"""Компактное хранение: история цен без лишнего индекса, неиспользуемый индекс сделок удалён.

SQLite: price_history становится таблицей WITHOUT ROWID — строки лежат прямо в дереве первичного ключа
(token_id, ts), и отдельный индекс-копия (почти такой же по размеру, как таблица) больше не нужен: история цен
занимает примерно вдвое меньше места. Данные переносятся целиком. В PostgreSQL таблица не меняется.
Индекс ix_trades_source не использовался ни одним запросом (колонка source у сделок пустая).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _rebuild_price_history(without_rowid: bool) -> None:
    tail = " WITHOUT ROWID" if without_rowid else ""
    op.execute(
        "CREATE TABLE price_history_new (token_id TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL, "
        f"PRIMARY KEY (token_id, ts)){tail}"
    )
    op.execute("INSERT INTO price_history_new (token_id, ts, price) SELECT token_id, ts, price FROM price_history")
    op.execute("DROP TABLE price_history")
    op.execute("ALTER TABLE price_history_new RENAME TO price_history")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "ix_trades_source" in {i["name"] for i in insp.get_indexes("trades")}:
        op.drop_index("ix_trades_source", table_name="trades")
    if bind.dialect.name == "sqlite":
        sql = bind.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='price_history'").scalar() or ""
        if "WITHOUT ROWID" not in sql.upper():
            _rebuild_price_history(True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _rebuild_price_history(False)
    op.create_index("ix_trades_source", "trades", ["source"])
