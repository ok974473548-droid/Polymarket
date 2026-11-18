"""Сборка позиций из сделок (ТЗ 6.1–6.2).

Позиция = (кошелёк, токен). Цена входа p — VWAP покупок. Выход R на акцию:
  * рынок разрешён → (выручка продаж + остаток × выплата) / купленные акции;
  * всё продано до разрешения → VWAP продаж;
  * иначе позиция открыта, R не определён.
held_to_resolution = до разрешения дожило ≥ 50% купленных акций.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .db import DB

EPS = 1e-9


def _build(trades: pd.DataFrame, markets: pd.DataFrame, tokens: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    trades = trades.sort_values("ts")
    buys = trades[trades.side == "BUY"]
    sells = trades[trades.side == "SELL"]
    g_b = buys.groupby(["wallet", "token_id"]).agg(
        shares_bought=("size_shares", "sum"), cost_usd=("size_usd", "sum"),
        opened_ts=("ts", "min"), last_buy_ts=("ts", "max"), n_buys=("trade_id", "count"))
    g_s = sells.groupby(["wallet", "token_id"]).agg(
        shares_sold=("size_shares", "sum"), proceeds_usd=("size_usd", "sum"),
        last_sell_ts=("ts", "max"), n_sells=("trade_id", "count"))
    pos = g_b.join(g_s, how="left").reset_index()
    pos[["shares_sold", "proceeds_usd", "n_sells"]] = pos[["shares_sold", "proceeds_usd", "n_sells"]].fillna(0)
    # продажи сверх купленного (акции получены split/merge или переводом) в позицию не входят —
    # и выручка за них тоже: иначе прибыль позиции завышается на продажу чужих акций
    over = pos["shares_sold"] > pos["shares_bought"]
    pos.loc[over, "proceeds_usd"] *= pos.loc[over, "shares_bought"] / pos.loc[over, "shares_sold"]
    pos["shares_sold"] = np.minimum(pos["shares_sold"], pos["shares_bought"])
    pos["avg_entry"] = pos["cost_usd"] / pos["shares_bought"].clip(lower=EPS)
    pos["remaining_shares"] = (pos["shares_bought"] - pos["shares_sold"]).clip(lower=0)

    tok = tokens[["token_id", "market_id", "payout"]].rename(columns={"market_id": "tok_market"})
    pos = pos.merge(tok, on="token_id", how="left")
    if "end_ts" not in markets:
        markets = markets.assign(end_ts=np.nan)
    mk = markets[["market_id", "event_id", "category", "status", "resolved_ts", "end_ts"]]
    pos = pos.merge(mk, left_on="tok_market", right_on="market_id", how="left")
    pos["market_id"] = pos["tok_market"]

    resolved = pos["payout"].notna()
    fully_sold = pos["remaining_shares"] <= pos["shares_bought"] * 1e-6
    sell_vwap = pos["proceeds_usd"] / pos["shares_sold"].clip(lower=EPS)

    pos["exit_value_per_share"] = np.where(
        resolved, (pos["proceeds_usd"] + pos["remaining_shares"] * pos["payout"].fillna(0)) / pos["shares_bought"].clip(lower=EPS),
        np.where(fully_sold, sell_vwap, np.nan))
    pos["pnl_usd"] = np.where(
        resolved, pos["proceeds_usd"] + pos["remaining_shares"] * pos["payout"].fillna(0) - pos["cost_usd"],
        np.where(fully_sold, pos["proceeds_usd"] - pos["cost_usd"], np.nan))
    pos["status"] = np.where(resolved, "resolved", np.where(fully_sold, "closed", "open"))
    pos["held_to_resolution"] = (resolved & (pos["remaining_shares"] >= 0.5 * pos["shares_bought"])).astype(int)
    # дата разрешения бывает неизвестна (Polymarket не отдал closedTime) — тогда срок рынка, а если и его нет,
    # последняя сделка. Без даты позиция выпадала из оценки кита целиком
    last_trade = pd.concat([pos["last_buy_ts"], pos["last_sell_ts"]], axis=1).max(axis=1)
    res_ts = pos["resolved_ts"].fillna(pos["end_ts"]).fillna(last_trade)
    pos["closed_ts"] = np.where(resolved & ~fully_sold, res_ts,
                                np.where(fully_sold & (pos["shares_sold"] > 0), pos["last_sell_ts"], np.nan))
    pos["n_trades"] = (pos["n_buys"] + pos["n_sells"]).astype(int)
    pos["position_id"] = pos["wallet"] + ":" + pos["token_id"]
    cols = ["position_id", "wallet", "token_id", "market_id", "event_id", "category", "opened_ts", "closed_ts",
            "shares_bought", "cost_usd", "avg_entry", "shares_sold", "proceeds_usd", "remaining_shares",
            "exit_value_per_share", "pnl_usd", "held_to_resolution", "n_trades", "status"]
    return pos[cols]


def build_positions(db: DB, wallets: list[str] | None = None) -> pd.DataFrame:
    """Пересобирает позиции (для всех или указанных кошельков) и пишет в таблицу positions."""
    cols = "trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd"
    if wallets:
        ph = ",".join("?" for _ in wallets)
        trades = db.frame(f"SELECT {cols} FROM trades WHERE wallet IN ({ph})", wallets)
        tok_filter = f"WHERE token_id IN (SELECT DISTINCT token_id FROM trades WHERE wallet IN ({ph}))"
        tokens = db.frame(f"SELECT token_id, market_id, payout FROM tokens {tok_filter}", wallets)
        markets = db.frame("SELECT market_id, event_id, category, status, resolved_ts, end_ts FROM markets WHERE market_id IN "
                           f"(SELECT market_id FROM tokens {tok_filter})", wallets)
    else:
        trades = db.frame(f"SELECT {cols} FROM trades")
        markets = db.frame("SELECT market_id, event_id, category, status, resolved_ts, end_ts FROM markets")
        tokens = db.frame("SELECT token_id, market_id, payout FROM tokens")
    if markets.empty:
        markets = pd.DataFrame(columns=["market_id", "event_id", "category", "status", "resolved_ts", "end_ts"])
    if tokens.empty:
        tokens = pd.DataFrame(columns=["token_id", "market_id", "payout"])
    pos = _build(trades, markets, tokens)
    if pos.empty:
        return pos
    ent = dict(db.frame("SELECT wallet, entity_id FROM entity_wallets").itertuples(index=False, name=None))
    pos["entity_id"] = pos["wallet"].map(ent).fillna(pos["wallet"])
    out = pos.copy()
    for k in ("opened_ts", "closed_ts"):
        out[k] = out[k].astype("Int64")                 # целые секунды, пропуски → NULL
    with db.tx():
        if wallets:
            ph = ",".join("?" for _ in wallets)
            db.execute(f"DELETE FROM positions WHERE wallet IN ({ph})", wallets)
        else:
            db.execute("DELETE FROM positions")
        db.upsert_df("positions", out, ["position_id"])
    return pos


def assign_entities(db: DB) -> None:
    """Проставляет entity_id в позициях после антисибил-кластеризации."""
    db.execute("UPDATE positions SET entity_id = COALESCE("
               "(SELECT entity_id FROM entity_wallets e WHERE e.wallet = positions.wallet), wallet)")
