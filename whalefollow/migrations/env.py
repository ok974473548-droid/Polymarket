"""Окружение Alembic: соединение передаёт приложение (config.attributes["connection"]);
для командной строки `alembic ...` — по WF_DB_URL."""

from __future__ import annotations

import os

from alembic import context

config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("WF_DB_URL", "sqlite:///whalefollow.db")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        context.configure(connection=conn, render_as_batch=True, transaction_per_migration=False)
        with context.begin_transaction():
            context.run_migrations()
        return
    import sqlalchemy as sa

    engine = sa.create_engine(_url())
    with engine.begin() as c:
        context.configure(connection=c, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run()
