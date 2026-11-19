"""Кого отслеживаем: данные собираются только по китам из списка пользователя.

Кит в списке — строка watchlist с режимом copy («Копировать») или watch («Только следить»).
Кит может быть одним кошельком или несколькими кошельками одного владельца (entity E-…).
Автопоиск новых китов по всей ленте Polymarket выключен по умолчанию: настройка auto_discover.
"""

from __future__ import annotations

from .config import DEFAULT_SETTINGS
from .db import DB, now_ts

ACTIVE_MODES = ("copy", "watch")


def auto_discover(db: DB) -> bool:
    v = db.get_setting("auto_discover")
    return bool(DEFAULT_SETTINGS.get("auto_discover") if v is None else v)


def tracked_entities(db: DB) -> list[str]:
    return [
        r["entity_id"]
        for r in db.query("SELECT entity_id FROM watchlist WHERE mode IN ('copy','watch') ORDER BY added_ts")
    ]


def tracked_wallets(db: DB) -> list[str]:
    """Все кошельки китов из списка (для объединённых игроков — все их кошельки)."""
    out: list[str] = []
    for e in tracked_entities(db):
        ws = [r["wallet"] for r in db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))]
        out += ws or ([e] if not e.startswith("E-") else [])
    return list(dict.fromkeys(w.lower() for w in out))


def migrate_watchlist(db: DB) -> int:
    """После объединения кошельков в одного игрока строки списка переносятся на игрока:
    режим берётся самый «активный» (copy важнее watch), выключенные темы объединяются."""
    import json

    moved = 0
    for r in db.query(
        "SELECT w.*, e.entity_id AS new_id FROM watchlist w JOIN entity_wallets e "
        "ON e.wallet=w.entity_id WHERE e.entity_id<>w.entity_id"
    ):
        cur = db.one("SELECT * FROM watchlist WHERE entity_id=?", (r["new_id"],))
        mode = r["mode"]
        if cur:
            rank = {"copy": 2, "watch": 1, "off": 0}
            mode = cur["mode"] if rank.get(cur["mode"], 0) >= rank.get(r["mode"], 0) else r["mode"]
        off = sorted(
            set(json.loads(r["categories_off"] or "[]")) | set(json.loads((cur or {}).get("categories_off") or "[]"))
        )
        with db.tx():
            db.upsert(
                "watchlist",
                {
                    "entity_id": r["new_id"],
                    "mode": mode,
                    "added_by": (cur or r)["added_by"] or "user",
                    "added_ts": min(x for x in ((cur or {}).get("added_ts"), r["added_ts"], now_ts()) if x),
                    "categories_off": json.dumps(off),
                },
                ["entity_id"],
            )
            db.execute("DELETE FROM watchlist WHERE entity_id=?", (r["entity_id"],))
        moved += 1
    # обратный случай: объединение кошельков распалось (связи больше не подтверждаются) — строка «E-…»
    # осталась без кошельков, и кит молча переставал отслеживаться. Переносим на основной кошелёк.
    for r in db.query(
        "SELECT w.* FROM watchlist w WHERE w.entity_id LIKE 'E-%' AND NOT EXISTS "
        "(SELECT 1 FROM entity_wallets e WHERE e.entity_id=w.entity_id)"
    ):
        wallet = r["entity_id"][2:]
        new_id = (db.one("SELECT entity_id FROM entity_wallets WHERE wallet=?", (wallet,)) or {}).get(
            "entity_id"
        ) or wallet
        cur = db.one("SELECT * FROM watchlist WHERE entity_id=?", (new_id,))
        rank = {"copy": 2, "watch": 1, "off": 0}
        mode = r["mode"] if not cur or rank.get(r["mode"], 0) > rank.get(cur["mode"], 0) else cur["mode"]
        with db.tx():
            db.upsert(
                "watchlist",
                {
                    "entity_id": new_id,
                    "mode": mode,
                    "added_by": (cur or r)["added_by"] or "user",
                    "added_ts": r["added_ts"],
                    "categories_off": r["categories_off"] or "[]",
                },
                ["entity_id"],
            )
            db.execute("DELETE FROM watchlist WHERE entity_id=?", (r["entity_id"],))
        moved += 1
    return moved


def usage(db: DB) -> dict:
    """Сколько данных относится к китам из списка и сколько — к остальным кошелькам."""
    wallets = tracked_wallets(db)
    total = int(db.scalar("SELECT COUNT(*) AS n FROM trades", default=0) or 0)
    mine = 0
    if wallets:
        ph = ",".join("?" for _ in wallets)
        mine = int(db.scalar(f"SELECT COUNT(*) AS n FROM trades WHERE wallet IN ({ph})", wallets, 0) or 0)
    others = int(db.scalar("SELECT COUNT(DISTINCT wallet) AS n FROM trades", default=0) or 0) - len(
        {w for w in wallets if db.one("SELECT 1 AS x FROM trades WHERE wallet=? LIMIT 1", (w,))}
    )
    return {
        "tracked_wallets": len(wallets),
        "trades_tracked": mine,
        "trades_other": total - mine,
        "other_wallets": max(0, others),
    }


def purge_untracked(db: DB, keep: list[str] | None = None, prices_before: int | None = None) -> dict:
    """Удаляет данные по кошелькам, которых нет в списке китов (и по их рынкам, где нет наших ставок).
    keep — кошельки, которые сейчас проверяются и ещё не добавлены. prices_before — историю цен и стаканы
    чужих рынков удалять только старше этого момента (ежедневная чистка не трогает свежие страницы событий)."""
    wallets = set(tracked_wallets(db)) | {w.lower() for w in (keep or [])}
    db.execute("CREATE TEMP TABLE IF NOT EXISTS keep_wallets (wallet TEXT PRIMARY KEY)")
    db.execute("DELETE FROM keep_wallets")
    db.executemany("INSERT INTO keep_wallets (wallet) VALUES (?)", [(w,) for w in wallets])
    out = {}
    with db.tx():
        for table, col in (
            ("trades", "wallet"),
            ("positions", "wallet"),
            ("activities", "wallet"),
            ("activity_sync", "wallet"),
            ("wallet_sync", "wallet"),
            ("chain_sync", "wallet"),
        ):
            before = db.scalar(f"SELECT COUNT(*) AS n FROM {table}", default=0) or 0
            db.execute(f"DELETE FROM {table} WHERE {col} NOT IN (SELECT wallet FROM keep_wallets)")
            out[table] = int(before - (db.scalar(f"SELECT COUNT(*) AS n FROM {table}", default=0) or 0))
        db.execute(
            "DELETE FROM transfers WHERE from_wallet NOT IN (SELECT wallet FROM keep_wallets) "
            "AND to_wallet NOT IN (SELECT wallet FROM keep_wallets)"
        )
        db.execute("DELETE FROM wallets WHERE wallet NOT IN (SELECT wallet FROM keep_wallets)")
        db.execute(
            "DELETE FROM wallet_links WHERE wallet_a NOT IN (SELECT wallet FROM keep_wallets) "
            "OR wallet_b NOT IN (SELECT wallet FROM keep_wallets)"
        )
        # «Перестать следить» (режим off) — кит больше не в списке, его оценки тоже не нужны
        ents = "SELECT entity_id FROM watchlist WHERE mode IN ('copy','watch')"
        db.execute("DELETE FROM entity_wallets WHERE wallet NOT IN (SELECT wallet FROM keep_wallets)")
        for t in ("wallet_scores", "wallet_category_scores", "wallet_flags", "wallet_reconcile", "score_inputs"):
            db.execute(
                f"DELETE FROM {t} WHERE entity_id NOT IN ({ents}) AND entity_id NOT IN "
                "(SELECT wallet FROM keep_wallets)"
            )
        # цены и стаканы — только по рынкам китов из списка и нашим ставкам
        live_tokens = (
            "SELECT token_id FROM trades UNION SELECT token_id FROM fills UNION SELECT token_id FROM signals "
            "UNION SELECT token_id FROM orders UNION SELECT token_id FROM positions"
        )
        old = " AND ts<?" if prices_before is not None else ""
        args = (prices_before,) if prices_before is not None else ()
        before = db.scalar("SELECT COUNT(*) AS n FROM price_history", default=0) or 0
        db.execute(f"DELETE FROM price_history WHERE token_id NOT IN ({live_tokens}){old}", args)
        out["price_history"] = int(before - (db.scalar("SELECT COUNT(*) AS n FROM price_history", default=0) or 0))
        db.execute(f"DELETE FROM orderbook_snapshots WHERE token_id NOT IN ({live_tokens}){old}", args)
    db.execute("DROP TABLE IF EXISTS keep_wallets")
    return out
