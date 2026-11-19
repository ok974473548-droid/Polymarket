"""Хранение данных: полное удаление кита и регулярная чистка базы, чтобы она не росла без предела.

• delete_whale — кит и все его данные: сделки, позиции, переводы, связи кошельков, оценки, сигналы без ставок,
  проверки, история цен и стаканы рынков, которые больше никому не нужны. Наши виртуальные ставки (orders/fills)
  и сигналы, по которым ставили, остаются — это история денег пользователя.
• cleanup — раз в сутки (задача housekeeping): данные игроков не из списка (после «Перестать следить»),
  старые снимки стаканов, новости, прогнозы других площадок, устаревший кэш площадок, старые закрытые рынки,
  прореживание теневых анализов, старые уведомления и живые цены.
• compact — SQLite возвращает освободившееся место файлу (инкрементальный VACUUM).
Настройки — ключ settings «retention» (сроки в днях); enabled=false выключает автоочистку.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from .db import DB

log = logging.getLogger(__name__)
DAY = 86400

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "untracked_grace_days": 3,  # данные проверенных, но не добавленных игроков живут столько дней
    "books_days": 30,  # снимки стаканов по расписанию
    "books_other_days": 180,  # снимки стаканов у сигналов и сделок (для «что было бы»)
    "orphan_prices_days": 30,  # история цен рынков, по которым нет сделок китов и наших ставок
    "news_days": 90,
    "forecasts_days": 90,
    "ext_stale_days": 14,  # кэш других площадок, который давно не обновлялся (рынок закрыт или удалён)
    "markets_days": 365,  # закрытые рынки без сделок, ставок и анализов
    "analyses_thin_days": 3,  # теневые анализы старше — один на исход в сутки (последний за день)
    "notifications_days": 180,
    "notifications_keep": 2000,
}


def settings(db: DB) -> dict[str, Any]:
    return {**DEFAULTS, **(db.get_setting("retention") or {})}


def _ph(xs: list[str]) -> str:
    return ",".join("?" for _ in xs)


def _count(db: DB, table: str) -> int:
    return int(db.scalar(f"SELECT COUNT(*) AS n FROM {table}", default=0) or 0)


def _has_table(db: DB, name: str) -> bool:
    if db.kind == "sqlite":
        return db.one("SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None
    return db.one("SELECT 1 AS x FROM information_schema.tables WHERE table_name=?", (name,)) is not None


def _delete(db: DB, out: dict, table: str, where: str, params: tuple | list = ()) -> None:
    """DELETE с подсчётом удалённых строк (одинаково для SQLite и PostgreSQL)."""
    if not _has_table(db, table):
        return
    before = _count(db, table)
    db.execute(f"DELETE FROM {table} WHERE {where}", tuple(params))
    n = before - _count(db, table)
    if n:
        out[table] = out.get(table, 0) + n


# ---------------------------------------------------------------- удаление кита
def whale_ids(db: DB, entity_id: str) -> tuple[str, list[str]]:
    """Кит (entity) и все его кошельки. Можно передать и кошелёк, входящий в объединённого игрока."""
    e = entity_id.strip()
    if not e.startswith("E-"):
        e = e.lower()
        e = (db.one("SELECT entity_id FROM entity_wallets WHERE wallet=?", (e,)) or {}).get("entity_id") or e
    wallets = [r["wallet"] for r in db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))]
    if not e.startswith("E-"):
        wallets.append(e)
    return e, sorted({w.lower() for w in wallets})


def delete_whale(db: DB, entity_id: str) -> dict:
    """Удаляет кита из списка и все его данные. Возвращает {таблица: удалено строк}."""
    ent, wallets = whale_ids(db, entity_id)
    ids = sorted({ent, entity_id.strip(), *wallets})
    out: dict[str, int] = {}
    w_ph, i_ph = _ph(wallets) if wallets else "NULL", _ph(ids)
    tokens = (
        [r["token_id"] for r in db.query(f"SELECT DISTINCT token_id FROM trades WHERE wallet IN ({w_ph})", wallets)]
        if wallets
        else []
    )
    # сигналы кита без наших ставок — вместе с решениями и оценками ИИ по ним
    sig_where = (
        f"(entity_id IN ({i_ph}) OR wallet IN ({w_ph})) AND signal_id NOT IN "
        "(SELECT signal_id FROM orders WHERE signal_id IS NOT NULL)"
    )
    sig_params = ids + wallets
    sigs = [r["signal_id"] for r in db.query(f"SELECT signal_id FROM signals WHERE {sig_where}", sig_params)]
    with db.tx():
        _delete(db, out, "watchlist", f"entity_id IN ({i_ph})", ids)
        for i in range(0, len(sigs), 500):
            chunk = sigs[i : i + 500]
            for t in ("decisions", "llm_estimates"):
                _delete(db, out, t, f"signal_id IN ({_ph(chunk)})", chunk)
            _delete(db, out, "signals", f"signal_id IN ({_ph(chunk)})", chunk)
            _delete(db, out, "notifications", f"ref IN ({_ph(chunk)})", chunk)  # ссылки вели бы в пустоту
        if wallets:
            for t in ("trades", "activities", "activity_sync", "wallet_sync", "chain_sync", "wallets"):
                _delete(db, out, t, f"wallet IN ({w_ph})", wallets)
            _delete(db, out, "positions", f"wallet IN ({w_ph}) OR entity_id IN ({i_ph})", wallets + ids)
            _delete(db, out, "transfers", f"from_wallet IN ({w_ph}) OR to_wallet IN ({w_ph})", wallets * 2)
            _delete(db, out, "wallet_links", f"wallet_a IN ({w_ph}) OR wallet_b IN ({w_ph})", wallets * 2)
            _delete(db, out, "check_jobs", f"LOWER(address) IN ({w_ph}) AND state IN ('done','error')", wallets)
        _delete(db, out, "entity_wallets", f"entity_id IN ({i_ph})", ids)
        for t in ("wallet_scores", "wallet_category_scores", "wallet_flags", "wallet_reconcile", "score_inputs"):
            _delete(db, out, t, f"entity_id IN ({i_ph})", ids)
        # цены и стаканы рынков кита — если они больше никому не нужны
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            orphan = f"token_id IN ({_ph(chunk)}) AND token_id NOT IN ({_used_tokens_sql()})"
            for t in ("price_history", "orderbook_snapshots", "live_quotes"):
                _delete(db, out, t, orphan, chunk)
    return out


def _used_tokens_sql() -> str:
    """Токены, история которых нужна: сделки китов, позиции, наши ставки и сигналы."""
    return (
        "SELECT token_id FROM trades UNION SELECT token_id FROM fills UNION SELECT token_id FROM orders "
        "UNION SELECT token_id FROM signals UNION SELECT token_id FROM positions"
    )


# ---------------------------------------------------------------- ежедневная чистка
def cleanup(db: DB, now: float | None = None, keep_wallets: list[str] | None = None) -> dict:
    """Чистка по срокам из настроек. keep_wallets — кошельки, которые сейчас проверяются."""
    from . import tracked

    cfg = settings(db)
    if not cfg.get("enabled", True):
        return {"enabled": False}
    now = int(now or time.time())
    out: dict[str, int] = {}

    def days(key: str) -> int:
        return now - int(float(cfg[key]) * DAY)

    # 1. Игроки не из списка (после «Перестать следить» или проверки без добавления) — если не включён автопоиск
    if not tracked.auto_discover(db):
        keep = {w.lower() for w in (keep_wallets or [])}
        if _has_table(db, "check_jobs"):
            keep |= {
                str(r["address"]).lower()
                for r in db.query("SELECT address FROM check_jobs WHERE started>=?", (days("untracked_grace_days"),))
            }
        for k, v in tracked.purge_untracked(db, sorted(keep), prices_before=days("orphan_prices_days")).items():
            if v:
                out[k] = out.get(k, 0) + v
    # 2. Стаканы: по расписанию — 30 дней, у сигналов и сделок — 180
    _delete(db, out, "orderbook_snapshots", "ts<? AND trigger='schedule'", (days("books_days"),))
    _delete(db, out, "orderbook_snapshots", "ts<?", (days("books_other_days"),))
    # 3. История цен рынков без сделок китов и наших ставок (страницы событий, проверки)
    _delete(
        db,
        out,
        "price_history",
        f"ts<? AND token_id NOT IN ({_used_tokens_sql()})",
        (days("orphan_prices_days"),),
    )
    # 4. Новости, прогнозы и кэш других площадок
    _delete(db, out, "news_items", "COALESCE(ts_fetched, ts_pub, 0)<?", (days("news_days"),))
    _delete(db, out, "forecasts", "COALESCE(ts_fetched, 0)<?", (days("forecasts_days"),))
    _delete(db, out, "ext_markets", "COALESCE(updated_ts, 0)<?", (days("ext_stale_days"),))
    # 5. Теневые анализы: старше 3 дней — по одному на исход в сутки (самопроверке хватает, вес исхода тот же)
    if _has_table(db, "event_analyses"):
        cut = days("analyses_thin_days")
        _delete(
            db,
            out,
            "event_analyses",
            "origin='shadow' AND ts<? AND id NOT IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
            "(PARTITION BY event_id, outcome_key, mode, ts / 86400 ORDER BY ts DESC) AS rn "
            "FROM event_analyses WHERE origin='shadow' AND ts<?) x WHERE rn=1)",
            (cut, cut),
        )
    # 6. Старые закрытые рынки, по которым нет сделок, позиций, ставок, сигналов, анализов и «Следить»
    _old_markets(db, out, days("markets_days"))
    # 7. Уведомления и живые цены
    _delete(db, out, "notifications", "ts<?", (days("notifications_days"),))
    keep_n = int(cfg["notifications_keep"])
    _delete(
        db,
        out,
        "notifications",
        "notif_id NOT IN (SELECT notif_id FROM (SELECT notif_id FROM notifications ORDER BY ts DESC LIMIT ?) x)",
        (keep_n,),
    )
    _delete(db, out, "live_quotes", "ts<?", (now - DAY,))
    return out


def _old_markets(db: DB, out: dict, before: int) -> None:
    used = [
        "SELECT market_id FROM trades WHERE market_id IS NOT NULL",
        "SELECT market_id FROM positions WHERE market_id IS NOT NULL",
        "SELECT market_id FROM signals WHERE market_id IS NOT NULL",
        "SELECT t.market_id FROM tokens t JOIN fills f ON f.token_id=t.token_id",
        "SELECT t.market_id FROM tokens t JOIN orders o ON o.token_id=t.token_id",
    ]
    if _has_table(db, "event_analyses"):
        used.append("SELECT market_id FROM event_analyses WHERE market_id IS NOT NULL")
    if _has_table(db, "event_watch"):
        used.append("SELECT m.market_id FROM markets m JOIN event_watch w ON w.event_id=m.event_id")
    old = (
        "SELECT market_id FROM markets WHERE status IN ('closed','resolved') "
        "AND COALESCE(resolved_ts, end_ts, updated_ts, 0)<? AND market_id NOT IN (" + " UNION ".join(used) + ")"
    )
    ids = [r["market_id"] for r in db.query(old, (before,))]
    with db.tx():
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            _delete(db, out, "tokens", f"market_id IN ({_ph(chunk)})", chunk)
            _delete(db, out, "markets", f"market_id IN ({_ph(chunk)})", chunk)
    _delete(
        db,
        out,
        "events",
        "COALESCE(end_ts, 0)<? AND event_id NOT IN (SELECT event_id FROM markets WHERE event_id IS NOT NULL)",
        (before,),
    )


# ---------------------------------------------------------------- место на диске
def compact(db: DB, min_free_mb: float = 8.0, full_ok: bool = False) -> dict:
    """SQLite: свободные страницы возвращаются файлу. Один раз (full_ok — при запуске, `whalefollow init-db`, пока
    процессы не пишут в базу) — полный VACUUM с включением режима auto_vacuum=INCREMENTAL; дальше, в ежедневной
    чистке, — быстрый incremental_vacuum, который не задерживает сигналы."""
    if db.kind != "sqlite" or db.path == ":memory:":
        return {"skipped": "не SQLite"}
    c = sqlite3.connect(db.path, timeout=120, isolation_level=None)
    try:
        page = c.execute("PRAGMA page_size").fetchone()[0]
        free = c.execute("PRAGMA freelist_count").fetchone()[0]
        mode = c.execute("PRAGMA auto_vacuum").fetchone()[0]
        res: dict[str, Any] = {"free_mb": round(free * page / 1e6, 1), "auto_vacuum": mode}
        if mode != 2 and not full_ok:
            res["vacuum"] = "ждёт перезапуска (init-db)"
        elif mode != 2:
            c.execute("PRAGMA auto_vacuum=INCREMENTAL")
            c.execute("VACUUM")  # один раз: без него режим не включится
            res["vacuum"] = "full"
        elif free * page / 1e6 >= min_free_mb:
            c.execute("PRAGMA incremental_vacuum").fetchall()  # прагма освобождает страницы по мере чтения строк
            res["vacuum"] = "incremental"
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        res["size_mb"] = round(c.execute("PRAGMA page_count").fetchone()[0] * page / 1e6, 1)
        return res
    finally:
        c.close()
