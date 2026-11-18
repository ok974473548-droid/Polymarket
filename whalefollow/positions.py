"""Сборка позиций из сделок (ТЗ 6.1–6.2). Движок — polars.

Позиция = (кошелёк, токен). Цена входа p — VWAP покупок. Выход R на акцию:
  * рынок разрешён → (выручка продаж + остаток × выплата) / купленные акции;
  * всё продано до разрешения → VWAP продаж;
  * иначе позиция открыта, R не определён.
held_to_resolution = до разрешения дожило ≥ 50% купленных акций.
"""

from __future__ import annotations

import polars as pl

from . import frames
from .db import DB

EPS = 1e-9
COLS = [
    "position_id",
    "wallet",
    "token_id",
    "market_id",
    "event_id",
    "category",
    "opened_ts",
    "closed_ts",
    "shares_bought",
    "cost_usd",
    "avg_entry",
    "shares_sold",
    "proceeds_usd",
    "remaining_shares",
    "exit_value_per_share",
    "pnl_usd",
    "held_to_resolution",
    "n_trades",
    "status",
]
MARKET_COLS = ["market_id", "event_id", "category", "status", "resolved_ts", "end_ts"]


def _build(trades: pl.DataFrame, markets: pl.DataFrame, tokens: pl.DataFrame) -> pl.DataFrame:
    """Позиции из сделок. Порядок строк — по (кошелёк, токен), как и раньше."""
    if trades.is_empty():
        return frames.empty(COLS)
    keys = ["wallet", "token_id"]
    g_b = (
        trades.filter(pl.col("side") == "BUY")
        .group_by(keys)
        .agg(
            shares_bought=pl.col("size_shares").sum(),
            cost_usd=pl.col("size_usd").sum(),
            opened_ts=pl.col("ts").min(),
            last_buy_ts=pl.col("ts").max(),
            n_buys=pl.len(),
        )
    )
    g_s = (
        trades.filter(pl.col("side") == "SELL")
        .group_by(keys)
        .agg(
            shares_sold=pl.col("size_shares").sum(),
            proceeds_usd=pl.col("size_usd").sum(),
            last_sell_ts=pl.col("ts").max(),
            n_sells=pl.len(),
        )
    )
    pos = (
        g_b.join(g_s, on=keys, how="left")
        .sort(keys)
        .with_columns(
            pl.col("shares_sold").fill_null(0.0),
            pl.col("proceeds_usd").fill_null(0.0),
            pl.col("n_sells").fill_null(0),
        )
    )
    # продажи сверх купленного (акции получены split/merge или переводом) в позицию не входят —
    # и выручка за них тоже: иначе прибыль позиции завышается на продажу чужих акций
    over = pl.col("shares_sold") > pl.col("shares_bought")
    pos = pos.with_columns(
        proceeds_usd=pl.when(over)
        .then(pl.col("proceeds_usd") * (pl.col("shares_bought") / pl.col("shares_sold")))
        .otherwise(pl.col("proceeds_usd")),
        shares_sold=pl.min_horizontal("shares_sold", "shares_bought"),
    ).with_columns(
        avg_entry=pl.col("cost_usd") / pl.col("shares_bought").clip(lower_bound=EPS),
        remaining_shares=(pl.col("shares_bought") - pl.col("shares_sold")).clip(lower_bound=0),
    )

    tok = tokens.select("token_id", pl.col("market_id").alias("tok_market"), pl.col("payout").cast(pl.Float64))
    pos = pos.join(tok, on="token_id", how="left", maintain_order="left")
    if "end_ts" not in markets.columns:
        markets = markets.with_columns(end_ts=pl.lit(None, dtype=pl.Int64))
    mk = frames.cast(markets.select(MARKET_COLS)).unique("market_id", keep="first", maintain_order=True)
    pos = pos.join(mk.rename({"market_id": "tok_market"}), on="tok_market", how="left", maintain_order="left")
    pos = pos.with_columns(market_id=pl.col("tok_market"))

    resolved = pl.col("payout").is_not_null() & ~pl.col("payout").is_nan()
    fully_sold = pl.col("remaining_shares") <= pl.col("shares_bought") * 1e-6
    sold_any = pl.col("shares_sold") > 0
    paid = pl.col("proceeds_usd") + pl.col("remaining_shares") * pl.col("payout").fill_null(0.0).fill_nan(0.0)
    sell_vwap = pl.col("proceeds_usd") / pl.col("shares_sold").clip(lower_bound=EPS)
    last_trade = pl.max_horizontal("last_buy_ts", "last_sell_ts")
    # дата разрешения бывает неизвестна (Polymarket не отдал closedTime) — тогда срок рынка, а если и его нет,
    # последняя сделка. Без даты позиция выпадала из оценки кита целиком
    res_ts = pl.coalesce("resolved_ts", "end_ts", last_trade)
    pos = pos.with_columns(
        exit_value_per_share=pl.when(resolved)
        .then(paid / pl.col("shares_bought").clip(lower_bound=EPS))
        .when(fully_sold)
        .then(sell_vwap)
        .otherwise(None),
        pnl_usd=pl.when(resolved)
        .then(paid - pl.col("cost_usd"))
        .when(fully_sold)
        .then(pl.col("proceeds_usd") - pl.col("cost_usd"))
        .otherwise(None),
        status=pl.when(resolved)
        .then(pl.lit("resolved"))
        .when(fully_sold)
        .then(pl.lit("closed"))
        .otherwise(pl.lit("open")),
        held_to_resolution=(resolved & (pl.col("remaining_shares") >= 0.5 * pl.col("shares_bought"))).cast(pl.Int64),
        closed_ts=pl.when(resolved & ~fully_sold)
        .then(res_ts)
        .when(fully_sold & sold_any)
        .then(pl.col("last_sell_ts"))
        .otherwise(None)
        .cast(pl.Int64),
        n_trades=(pl.col("n_buys") + pl.col("n_sells")).cast(pl.Int64),
        position_id=pl.col("wallet") + ":" + pl.col("token_id"),
    )
    return frames.cast(pos.select(COLS))


def build_positions(db: DB, wallets: list[str] | None = None) -> pl.DataFrame:
    """Пересобирает позиции (для всех или указанных кошельков) и пишет в таблицу positions."""
    cols = "trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd"
    if wallets:
        ph = ",".join("?" for _ in wallets)
        trades = frames.read_history(db, f"SELECT {cols} FROM trades WHERE wallet IN ({ph})", wallets)
        tok_filter = f"WHERE token_id IN (SELECT DISTINCT token_id FROM trades WHERE wallet IN ({ph}))"
        tokens = frames.read(db, f"SELECT token_id, market_id, payout FROM tokens {tok_filter}", wallets)
        markets = frames.read(
            db,
            "SELECT market_id, event_id, category, status, resolved_ts, end_ts FROM markets WHERE market_id IN "
            f"(SELECT market_id FROM tokens {tok_filter})",
            wallets,
        )
    else:
        trades = frames.read_history(db, f"SELECT {cols} FROM trades")
        markets = frames.read(db, "SELECT market_id, event_id, category, status, resolved_ts, end_ts FROM markets")
        tokens = frames.read(db, "SELECT token_id, market_id, payout FROM tokens")
    pos = _build(trades, markets, tokens)
    if pos.is_empty():
        return pos
    ent = frames.read(db, "SELECT wallet, entity_id FROM entity_wallets").unique("wallet", keep="last")
    pos = pos.join(ent, on="wallet", how="left", maintain_order="left").with_columns(
        entity_id=pl.coalesce("entity_id", "wallet")
    )
    with db.tx():
        if wallets:
            ph = ",".join("?" for _ in wallets)
            db.execute(f"DELETE FROM positions WHERE wallet IN ({ph})", wallets)
        else:
            db.execute("DELETE FROM positions")
        db.upsert_frame("positions", pos, ["position_id"])
    return pos


def assign_entities(db: DB) -> None:
    """Проставляет entity_id в позициях после антисибил-кластеризации."""
    db.execute(
        "UPDATE positions SET entity_id = COALESCE("
        "(SELECT entity_id FROM entity_wallets e WHERE e.wallet = positions.wallet), wallet)"
    )
