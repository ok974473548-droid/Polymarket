"""Классификация игроков (ТЗ 6.6, 7.1, 7.3, 8.4).

Классы: normal, maker_arb, insider_suspect, favorite_grinder.
Флаги пишутся в wallet_flags со значениями, чтобы решение можно было разобрать.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Params


MERGE_SPLIT_SHARE = 0.2      # split/merge больше чем в 20% рынков — признак маркет-мейкера


def maker_flags(trades: pd.DataFrame, pos: pd.DataFrame, params: Params) -> dict:
    """F1–F5 для маркет-мейкеров и арбитражёров."""
    flags = {"F1_two_sided": 0.0, "F2_short_hold_h": float("nan"), "F3_trades_per_market": 0.0,
             "F4_net_turnover": float("nan"), "F5_merge_split": 0.0}
    if trades.empty:
        return flags
    # split/merge из журнала кошелька — отдельный признак F5; в F1–F4 не входят (split = «покупка» всех исходов
    # сразу, иначе каждый split выглядел бы двусторонней торговлей)
    if "source" in trades:
        act = trades["source"].fillna("").astype(str).isin(["split", "merge"])
        n_m = max(trades["market_id"].nunique(), 1)
        flags["F5_merge_split"] = float(trades.loc[act, "market_id"].nunique()) / n_m
        trades = trades[~act]
        if trades.empty:
            return flags
    by_market = trades.groupby("market_id")
    # двусторонний рынок: покупал оба исхода ИЛИ по одному исходу и покупал, и продавал (без цикла по рынкам)
    both_tokens = trades[trades["side"] == "BUY"].groupby("market_id")["token_id"].nunique() >= 2
    buy_and_sell = trades.groupby(["market_id", "token_id"])["side"].nunique().eq(2).groupby(level=0).any()
    two_sided = both_tokens.reindex(buy_and_sell.index, fill_value=False) | buy_and_sell
    flags["F1_two_sided"] = float(two_sided.sum()) / max(by_market.ngroups, 1)
    sold = pos[(pos["status"] == "closed") & pos["closed_ts"].notna()]
    if len(sold):
        flags["F2_short_hold_h"] = float(np.median((sold["closed_ts"] - sold["opened_ts"]) / 3600))
    flags["F3_trades_per_market"] = float(by_market.size().median())
    turnover = float(trades["size_usd"].sum())
    if turnover > 0 and len(pos):
        net = float((pos["remaining_shares"].fillna(0) * pos["avg_entry"]).sum())
        flags["F4_net_turnover"] = net / turnover
    return flags


def is_maker(flags: dict, params: Params) -> bool:
    hits = 0
    hits += flags["F1_two_sided"] > params.maker_two_sided_share
    hits += bool(np.isfinite(flags["F2_short_hold_h"]) and flags["F2_short_hold_h"] < params.maker_median_hold_h)
    hits += flags["F3_trades_per_market"] > params.maker_trades_per_market
    hits += bool(np.isfinite(flags["F4_net_turnover"]) and flags["F4_net_turnover"] < params.maker_net_turnover)
    hits += flags["F5_merge_split"] > MERGE_SPLIT_SHARE
    return hits >= params.maker_flags_needed


def favorite_flags(closed: pd.DataFrame, params: Params) -> dict:
    if closed.empty:
        return {"fav_cost_share": 0.0, "fav_pnl_share": 0.0}
    fav = closed["avg_entry"] >= params.favorite_price
    total_cost = closed["cost_usd"].sum()
    pos_pnl = closed.loc[closed["pnl_usd"] > 0, "pnl_usd"]
    fav_pnl = closed.loc[fav & (closed["pnl_usd"] > 0), "pnl_usd"].sum()
    return {"fav_cost_share": float(closed.loc[fav, "cost_usd"].sum() / total_cost) if total_cost else 0.0,
            "fav_pnl_share": float(fav_pnl / pos_pnl.sum()) if pos_pnl.sum() > 0 else 0.0}


def is_favorite_grinder(f: dict, params: Params) -> bool:
    return f["fav_cost_share"] >= params.favorite_cost_share or f["fav_pnl_share"] > params.favorite_pnl_share


def insider_flags(pos: pd.DataFrame, n_clusters: int, first_seen_ts: float | None,
                  market_volume: dict, params: Params) -> dict:
    """I1–I5 (7.3). Глубину стакана на момент ставки в истории обычно не восстановить,
    поэтому I3 считается по доле в объёме рынка."""
    f = {"I1_wallet_age_days": float("nan"), "I2_top_event_share": 0.0, "I3_size_vs_volume": 0.0,
         "I4_jump_share": 0.0, "I5_clusters": float(n_clusters)}
    if pos.empty:
        return f
    big = pos.loc[pos["cost_usd"].idxmax()]
    if first_seen_ts is not None and np.isfinite(first_seen_ts):
        f["I1_wallet_age_days"] = float((big["opened_ts"] - first_seen_ts) / 86400)
    key = pos["event_id"].fillna(pos["market_id"])
    total = pos["cost_usd"].sum()
    f["I2_top_event_share"] = float(pos.groupby(key)["cost_usd"].sum().max() / total) if total else 0.0
    vol = market_volume.get(big["market_id"]) or 0.0
    f["I3_size_vs_volume"] = float(big["cost_usd"] / vol) if vol > 0 else 0.0
    if "clv_24h" in pos and pos["clv_24h"].notna().any():
        jumps = pos["clv_24h"].dropna() >= params.insider_jump_pp
        f["I4_jump_share"] = float(jumps.mean())
    return f


def is_insider(f: dict, params: Params) -> bool:
    hits = 0
    hits += bool(np.isfinite(f["I1_wallet_age_days"]) and f["I1_wallet_age_days"] < params.insider_wallet_age_days)
    hits += f["I2_top_event_share"] > params.insider_single_event_share
    hits += f["I3_size_vs_volume"] > params.insider_big_vs_volume
    hits += f["I4_jump_share"] > params.insider_jump_share
    hits += f["I5_clusters"] < params.insider_min_clusters
    return hits >= params.insider_flags_needed


def hold_profile(closed: pd.DataFrame) -> dict:
    """Доля удержания до разрешения и медианный срок удержания (8.4)."""
    if closed.empty:
        return {"hold_share": float("nan"), "median_hold_h": float("nan")}
    hold = (closed["closed_ts"] - closed["opened_ts"]) / 3600
    return {"hold_share": float(closed["held_to_resolution"].mean()),
            "median_hold_h": float(np.nanmedian(hold)) if hold.notna().any() else float("nan")}


def classify(trades: pd.DataFrame, pos: pd.DataFrame, closed: pd.DataFrame, n_clusters: int,
             first_seen_ts, market_volume: dict, params: Params) -> tuple[str, dict]:
    mf = maker_flags(trades, pos, params)
    ff = favorite_flags(closed, params)
    inf = insider_flags(pos, n_clusters, first_seen_ts, market_volume, params)
    flags = {**mf, **ff, **inf}
    if is_maker(mf, params):
        return "maker_arb", flags
    if is_insider(inf, params):
        return "insider_suspect", flags
    if is_favorite_grinder(ff, params):
        return "favorite_grinder", flags
    return "normal", flags
