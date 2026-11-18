"""Чтение данных из базы в таблицы polars (аналитический движок, этап 3).

  read(db, sql)          — обычный запрос: строки курсора сразу в polars (без промежуточных словарей).
  read_history(db, sql)  — большие таблицы истории (цены, сделки, снимки стаканов): DuckDB читает файл SQLite
                           напрямую (расширение sqlite) — в разы быстрее построчного чтения. Если DuckDB или его
                           расширение недоступны (нет сети при первой установке, PostgreSQL, база в памяти) —
                           тот же результат обычным путём. Отключить: WF_DUCKDB=off.

Типы колонок одинаковы на обоих путях (SCHEMA), поэтому расчёт не зависит от того, как прочитаны данные.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from typing import Any

import polars as pl

log = logging.getLogger(__name__)

INT, F, S = pl.Int64, pl.Float64, pl.Utf8
# Типы по имени колонки (имена в схеме согласованы между таблицами)
SCHEMA: dict[str, Any] = {
    **dict.fromkeys(
        [
            "ts",
            "opened_ts",
            "closed_ts",
            "resolved_ts",
            "end_ts",
            "first_seen_ts",
            "first_funded_ts",
            "last_buy_ts",
            "last_sell_ts",
            "held_to_resolution",
            "n_trades",
            "computed_at",
            "n_clusters",
            "last_trade_ts",
            "is_maker",
            "neg_risk",
            "ts_fetched",
        ],
        INT,
    ),
    **dict.fromkeys(
        [
            "price",
            "size_shares",
            "size_usd",
            "payout",
            "volume_usd",
            "liquidity_usd",
            "shares_bought",
            "cost_usd",
            "avg_entry",
            "shares_sold",
            "proceeds_usd",
            "remaining_shares",
            "exit_value_per_share",
            "pnl_usd",
            "amount",
            "fee_rate",
            "fee_bps",
            "edge_raw",
            "edge_shrunk",
            "edge_ci_lo",
            "edge_ci_hi",
            "clv_1h",
            "clv_24h",
            "clv_ci_lo",
            "pvalue",
            "qvalue",
            "edge_half1",
            "edge_half2",
            "decay_ratio",
            "hold_share",
            "median_hold_h",
            "return_90d",
            "score",
            "total_cost",
            "history_days",
            "confidence",
            "strength",
            "value",
        ],
        F,
    ),
    **dict.fromkeys(
        [
            "trade_id",
            "wallet",
            "token_id",
            "market_id",
            "event_id",
            "category",
            "status",
            "side",
            "source",
            "position_id",
            "entity_id",
            "owner_eoa",
            "funder_wallet",
            "from_wallet",
            "to_wallet",
            "asks",
            "bids",
            "tx_hash",
            "question",
            "outcome_label",
        ],
        S,
    ),
}


def _dtype(col: str) -> Any:
    return SCHEMA.get(col, S)


def empty(cols: Sequence[str]) -> pl.DataFrame:
    return pl.DataFrame(schema={c: _dtype(c) for c in cols})


def from_rows(rows: Sequence[Sequence[Any]], cols: Sequence[str]) -> pl.DataFrame:
    """Строки (кортежи) → таблица с типами из SCHEMA; неизвестные колонки — по данным."""
    if not rows:
        return empty(cols)
    known = {c: SCHEMA[c] for c in cols if c in SCHEMA}
    return pl.DataFrame(
        rows, schema=list(cols), schema_overrides=known, orient="row", strict=False, infer_schema_length=None
    )


def cast(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([pl.col(c).cast(SCHEMA[c], strict=False) for c in df.columns if c in SCHEMA])


def read(db: Any, sql: str, params: Sequence[Any] = ()) -> pl.DataFrame:
    cols, rows = db.rows(sql, params)
    return from_rows(rows, cols)


# ---------------------------------------------------------------- DuckDB: история напрямую из файла SQLite
_duck_ok: bool | None = None
_duck_lock = threading.Lock()


def _duckdb_available() -> bool:
    global _duck_ok
    if os.environ.get("WF_DUCKDB", "on").strip().lower() in ("0", "off", "false", "no"):
        return False
    with _duck_lock:
        if _duck_ok is None:
            try:
                import duckdb

                c = duckdb.connect()
                try:
                    c.execute("LOAD sqlite")
                except Exception:
                    c.execute("INSTALL sqlite")
                    c.execute("LOAD sqlite")
                c.close()
                _duck_ok = True
            except Exception as exc:
                log.info("DuckDB недоступен, история читается обычным путём: %s", exc)
                _duck_ok = False
        return _duck_ok


def read_history(db: Any, sql: str, params: Sequence[Any] = ()) -> pl.DataFrame:
    """Большие таблицы истории. Результат тот же, что у read(), только быстрее на файле SQLite."""
    if db.kind == "sqlite" and db.path != ":memory:" and _duckdb_available():
        try:
            import duckdb

            con = duckdb.connect()
            try:
                con.execute("LOAD sqlite")
                path = os.path.abspath(db.path).replace("'", "''")
                con.execute(f"ATTACH '{path}' AS wf (TYPE sqlite, READ_ONLY)")
                con.execute("USE wf")
                out = con.sql(sql, params=list(params) or None).pl()
            finally:
                con.close()
            return cast(out)
        except Exception as exc:
            log.info("DuckDB: %s — читаю обычным путём", exc)
    return read(db, sql, params)
