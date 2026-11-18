"""Миграции базы (Alembic).

Стартовая миграция 0001 — схема версии 0.13 (`whalefollow/schema.sql` + колонки, которые раньше дописывались
при запуске). Любое следующее изменение схемы — новый файл в `versions/` (`alembic revision -m "..."`
из корня проекта); при запуске приложения база обновляется до последней версии сама.

Старые базы (до перехода на Alembic) при первом запуске приводятся к стартовой версии — недостающие
колонки и индексы дописываются, данные не трогаются — и помечаются как «на версии 0001».
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
VERSION_TABLE = "alembic_version"


@lru_cache(maxsize=4)
def _script(location: str = str(HERE)):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", location)
    return cfg, ScriptDirectory.from_config(cfg)


def head(location: str | None = None) -> str:
    return _script(location or str(HERE))[1].get_current_head()


class _NoClose:
    """Обёртка над уже открытым соединением sqlite3: Alembic/SQLAlchemy не должны его закрывать."""

    def __init__(self, raw: Any):
        self._raw = raw

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def _engine(db: Any):
    import sqlalchemy as sa
    from sqlalchemy.pool import StaticPool

    if db.kind == "pg":
        url = db.url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
            "postgres://", "postgresql+psycopg://", 1
        )
        return sa.create_engine(url), True
    raw = _NoClose(db.conn)
    return sa.create_engine("sqlite://", creator=lambda: raw, poolclass=StaticPool), False


def current(db: Any) -> str | None:
    """Версия схемы базы (None — база без отметки Alembic)."""
    try:
        row = db.one(f"SELECT version_num FROM {VERSION_TABLE}")
    except Exception:
        return None
    return row["version_num"] if row else None


def _has_tables(db: Any) -> bool:
    if db.kind == "sqlite":
        return bool(db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='markets'"))
    return bool(db.one("SELECT table_name FROM information_schema.tables WHERE table_name='markets'"))


def upgrade(db: Any, revision: str = "head", location: str | None = None) -> str:
    """Обновляет базу до revision (по умолчанию — последней). Быстро, если обновлять нечего.
    location — другая папка миграций (для тестов)."""
    location = location or str(HERE)
    cur = current(db)
    target = head(location) if revision == "head" else revision
    if cur == target:
        return cur
    legacy = cur is None and _has_tables(db)
    from alembic import command

    cfg, _ = _script(location)
    engine, dispose = _engine(db)
    try:
        with db._lock, engine.begin() as conn:
            cfg.attributes["connection"] = conn
            try:
                command.upgrade(cfg, revision)
            finally:
                cfg.attributes.pop("connection", None)
    finally:
        if dispose:
            engine.dispose()
    new = current(db) or target
    if legacy:
        log.info("База без отметки версии приведена к стартовой схеме и обновлена до %s", new)
    elif cur != new:
        log.info("Схема базы обновлена: %s → %s", cur or "пусто", new)
    return new
