"""Тонкий слой БД: SQLite (по умолчанию) или PostgreSQL (если установлен psycopg).

URL: "sqlite:///path/to.db", "sqlite://:memory:" или "postgresql://user:pass@host/db".
SQL в коде пишется с плейсхолдером "?" — для PostgreSQL он переводится в "%s".
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from typing import Any


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


def now_ts() -> int:
    return int(time.time())


SQLITE_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",  # в режиме WAL безопасно и в разы быстрее записи по одной строке
    "PRAGMA busy_timeout=60000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-65536",  # 64 МБ кэша страниц на соединение
    "PRAGMA mmap_size=268435456",
)


class DB:
    """Для файла SQLite у каждого потока своё соединение: экраны читают, пока агент или
    пересчёт пишут (WAL). Запись идёт под общим замком процесса — без конкуренции писателей.
    Для ":memory:" (тесты) и PostgreSQL — одно соединение под замком, как раньше."""

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("WF_DB_URL", "sqlite:///whalefollow.db")
        self._lock = threading.RLock()  # замок записи (и всех операций для общего соединения)
        self._local = threading.local()
        self._shared = None
        self._shared_depth = 0
        self.listeners: list = []
        self._published = 0  # подписчики на уведомления (Telegram и т.п.)
        if self.url.startswith("postgres"):
            import psycopg  # type: ignore  # устанавливается extras [postgres]
            from psycopg.rows import dict_row  # type: ignore

            self.kind = "pg"
            self._shared = psycopg.connect(self.url, row_factory=dict_row, autocommit=True)
        else:
            path = self.url.split("sqlite://", 1)[-1]
            path = path[1:] if path.startswith("/") else path
            self.kind = "sqlite"
            self.path = path or ":memory:"
            if self.path == ":memory:":
                self._shared = self._connect()
            else:
                c = self._connect()
                c.execute("PRAGMA journal_mode=WAL")
                self._local.conn = c

    def _connect(self):
        c = sqlite3.connect(self.path, check_same_thread=False, timeout=60)
        c.row_factory = sqlite3.Row
        for p in SQLITE_PRAGMAS:
            c.execute(p)
        return c

    @property
    def per_thread(self) -> bool:
        return self._shared is None

    @property
    def conn(self):
        if self._shared is not None:
            return self._shared
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._connect()
        return c

    @property
    def _tx_depth(self) -> int:
        return self._shared_depth if self._shared is not None else getattr(self._local, "depth", 0)

    @_tx_depth.setter
    def _tx_depth(self, v: int) -> None:
        if self._shared is not None:
            self._shared_depth = v
        else:
            self._local.depth = v

    @contextmanager
    def _read(self):
        """Чтение: со своим соединением потока — без замка, с общим — под замком."""
        if self.per_thread:
            yield self.conn
        else:
            with self._lock:
                yield self.conn

    # --- базовые операции ---
    def _sql(self, sql: str) -> str:
        # PostgreSQL (psycopg): «?» → «%s», а знак процента в тексте запроса (LIKE '%…') удваивается
        return sql.replace("%", "%%").replace("?", "%s") if self.kind == "pg" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._execute(sql, params)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.conn.execute(self._sql(sql), tuple(params))
        if self.kind == "sqlite" and self._tx_depth == 0:
            self.conn.commit()

    def execute_count(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполняет запрос и возвращает число затронутых строк (атомарный «захват» задачи и т.п.)."""
        with self._lock:
            if self.kind == "pg":
                with self.conn.cursor() as cur:
                    cur.execute(self._sql(sql), tuple(params))
                    return max(cur.rowcount, 0)
            cur = self.conn.execute(sql, tuple(params))
            if self._tx_depth == 0:
                self.conn.commit()
            return max(cur.rowcount, 0)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._lock:
            return self._executemany(sql, rows)

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = [tuple(r) for r in rows]
        if not rows:
            return 0
        if self.kind == "pg":
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.executemany(sql, rows)
            if self._tx_depth == 0:
                self.conn.commit()
        return len(rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._read() as conn:
            cur = conn.execute(self._sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def rows(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[tuple]]:
        """(колонки, строки-кортежи) — быстрый путь для таблиц polars (без словаря на строку)."""
        with self._read() as conn:
            if self.kind == "pg":
                from psycopg.rows import tuple_row  # type: ignore

                with conn.cursor(row_factory=tuple_row) as cur:
                    cur.execute(self._sql(sql), tuple(params))
                    return [d[0] for d in cur.description or []], list(cur.fetchall())
            cur = conn.cursor()
            cur.row_factory = None
            cur.execute(sql, tuple(params))
            return [d[0] for d in cur.description or []], cur.fetchall()

    def frame(self, sql: str, params: Sequence[Any] = ()):
        """Результат запроса сразу в таблицу polars (типы колонок — frames.SCHEMA)."""
        from .frames import read

        return read(self, sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default=None):
        row = self.one(sql, params)
        if not row:
            return default
        val = next(iter(row.values()))
        return default if val is None else val

    @contextmanager
    def tx(self):
        with self._lock:
            if self.kind == "pg":
                with self.conn.transaction():
                    yield self
                return
            with self._sqlite_tx():
                yield self

    @contextmanager
    def _sqlite_tx(self):
        self._tx_depth += 1
        try:
            yield self
            if self._tx_depth == 1:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._tx_depth -= 1

    def _columns(self, table: str) -> set[str]:
        if self.kind == "sqlite":
            return {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
        return {
            r["column_name"]
            for r in self.query("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))
        }

    def init_schema(self) -> str:
        """Создаёт или обновляет схему миграциями Alembic; возвращает версию схемы."""
        from .migrations import upgrade

        return upgrade(self)

    def schema_version(self) -> str | None:
        from .migrations import current

        return current(self)

    def upsert(self, table: str, row: dict, keys: Sequence[str]) -> None:
        cols = list(row)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys) or f"{keys[0]}=excluded.{keys[0]}"
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {updates}"
        )
        self.execute(sql, [row[c] for c in cols])

    def upsert_many(self, table: str, rows: list[dict], keys: Sequence[str]) -> int:
        if not rows:
            return 0
        cols = list(rows[0])
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys) or f"{keys[0]}=excluded.{keys[0]}"
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {updates}"
        )
        return self.executemany(sql, ([r[c] for c in cols] for r in rows))

    def upsert_frame(self, table: str, df: Any, keys: Sequence[str]) -> int:
        """upsert из таблицы polars без перекладки в словари (быстро для сотен тысяч строк). NaN → NULL."""
        if df is None or df.height == 0:
            return 0
        import polars as pl

        floats = [c for c, t in df.schema.items() if t in (pl.Float32, pl.Float64)]
        if floats:
            df = df.with_columns([pl.col(c).fill_nan(None) for c in floats])
        cols = list(df.columns)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys) or f"{keys[0]}=excluded.{keys[0]}"
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {updates}"
        )
        return self.executemany(sql, df.iter_rows())

    def insert_ignore(self, table: str, rows: list[dict]) -> int:
        """Идемпотентная вставка: повторная загрузка не создаёт дублей (правило 1 раздела 4)."""
        if not rows:
            return 0
        cols = list(rows[0])
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        data = [tuple(r[c] for c in cols) for r in rows]
        with self._lock:
            return self._insert_ignore(sql, data)

    def _insert_ignore(self, sql: str, data: list[tuple]) -> int:
        if self.kind == "pg":
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), data)
                return max(cur.rowcount, 0)
        before = self.conn.total_changes
        self.conn.executemany(sql, data)
        if self._tx_depth == 0:
            self.conn.commit()
        return self.conn.total_changes - before

    # --- настройки и состояние ---
    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        self.upsert("settings", {"key": key, "value": json.dumps(value)}, ["key"])

    def get_state(self, key: str, default=None):
        row = self.one("SELECT value FROM risk_state WHERE key=?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_state(self, key: str, value) -> None:
        self.upsert("risk_state", {"key": key, "value": json.dumps(value), "updated_ts": now_ts()}, ["key"])

    @contextmanager
    def job(self, name: str):
        """Журнал запусков задач (job_runs)."""
        run = {
            "run_id": new_id("job_"),
            "job": name,
            "started_at": now_ts(),
            "finished_at": None,
            "status": "running",
            "n_rows": 0,
            "error": None,
        }
        self.upsert("job_runs", run, ["run_id"])
        counter = {"rows": 0}
        try:
            yield counter
            run.update(status="ok", finished_at=now_ts(), n_rows=counter["rows"])
        except Exception as exc:
            run.update(status="error", finished_at=now_ts(), error=repr(exc)[:500])
            raise
        finally:
            self.upsert("job_runs", run, ["run_id"])

    def publish(self, kind: str, **data: Any) -> None:
        """Событие для живого интерфейса: сразу — страницам этого процесса (шина в памяти),
        и в таблицу live_events — страницам, которые обслуживает другой процесс (api)."""
        from .events import bus

        bus.publish(kind, **data)
        try:
            self.execute(
                "INSERT INTO live_events (ts, kind, data) VALUES (?, ?, ?)",
                (time.time(), kind, json.dumps(data, ensure_ascii=False, default=str)),
            )
            self._published += 1
            if self._published % 500 == 0:  # события нужны страницам лишь несколько секунд — храним час
                self.execute("DELETE FROM live_events WHERE ts<?", (time.time() - 3600,))
        except Exception:
            pass

    def notify(self, kind: str, title: str, body: str = "", ref: str = "", ts: int | None = None) -> None:
        row = {
            "notif_id": new_id("n_"),
            "ts": ts or now_ts(),
            "kind": kind,
            "title": title,
            "body": body,
            "ref": ref,
            "is_read": 0,
        }
        self.upsert("notifications", row, ["notif_id"])
        self.publish("notification", about=kind)
        for fn in self.listeners:
            try:
                fn(row)
            except Exception:  # уведомление не должно ронять агента
                pass
