"""Классификация игроков (ТЗ 6.6, 7.1, 7.3, 8.4). Движок — polars + numpy.

Классы: normal, maker_arb, insider_suspect, favorite_grinder.
Флаги пишутся в wallet_flags со значениями, чтобы решение можно было разобрать.

Признаки по сделкам (F1, F3, F5, оборот) считаются сразу для всех игроков одной группировкой polars;
признаки по позициям — на массивах numpy одного игрока (Cols: колонка → массив).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..config import Params

MERGE_SPLIT_SHARE = 0.2  # split/merge больше чем в 20% рынков — признак маркет-мейкера
Cols = dict[str, np.ndarray]
NAN = float("nan")


def cols(df: pl.DataFrame, names: list[str] | None = None) -> Cols:
    """Таблица → словарь массивов numpy (пропуски чисел — NaN, строк — None)."""
    out: Cols = {}
    for c in names or df.columns:
        s = df[c]
        if s.dtype in (pl.Utf8, pl.Object, pl.Null):
            out[c] = np.array(s.to_list(), dtype=object)
        else:
            out[c] = s.cast(pl.Float64).fill_nan(None).to_numpy().astype(float)
    return out


def default_maker_flags() -> dict:
    return {
        "F1_two_sided": 0.0,
        "F2_short_hold_h": NAN,
        "F3_trades_per_market": 0.0,
        "F4_net_turnover": NAN,
        "F5_merge_split": 0.0,
    }


def maker_trade_flags(trades: pl.DataFrame) -> dict[str, dict]:
    """F1, F3, F5 и оборот по сделкам всех игроков (колонка entity_id). split/merge из журнала кошелька —
    отдельный признак F5; в F1–F4 не входят (split = «покупка» всех исходов сразу, иначе каждый split
    выглядел бы двусторонней торговлей)."""
    if trades.is_empty():
        return {}
    has_src = "source" in trades.columns
    t = trades.with_columns(
        _act=pl.col("source").fill_null("").cast(pl.Utf8).is_in(["split", "merge"]) if has_src else pl.lit(False)
    )
    base = t.group_by("entity_id").agg(
        n_m_all=pl.col("market_id").drop_nulls().n_unique(),
        n_m_act=pl.col("market_id").filter(pl.col("_act")).drop_nulls().n_unique(),
        n_rest=(~pl.col("_act")).sum(),
        turnover=pl.col("size_usd").filter(~pl.col("_act")).sum(),
    )
    rest = t.filter(~pl.col("_act") & pl.col("market_id").is_not_null())
    per_market = rest.group_by("entity_id", "market_id").agg(
        size=pl.len(),
        both_tokens=pl.col("token_id").filter(pl.col("side") == "BUY").drop_nulls().n_unique() >= 2,
    )
    buy_sell = (
        rest.filter(pl.col("token_id").is_not_null())
        .group_by("entity_id", "market_id", "token_id")
        .agg(bs=pl.col("side").n_unique() == 2)
        .group_by("entity_id", "market_id")
        .agg(bs=pl.col("bs").any())
    )
    two = (
        buy_sell.join(
            per_market.select("entity_id", "market_id", "both_tokens"), on=["entity_id", "market_id"], how="left"
        )
        .group_by("entity_id")
        .agg(two=(pl.col("bs") | pl.col("both_tokens").fill_null(False)).sum())
    )
    pm = per_market.group_by("entity_id").agg(n_markets=pl.len(), f3=pl.col("size").median())
    table = base.join(pm, on="entity_id", how="left").join(two, on="entity_id", how="left")
    out: dict[str, dict] = {}
    for r in table.iter_rows(named=True):
        f = default_maker_flags()
        f["F5_merge_split"] = float(r["n_m_act"]) / max(r["n_m_all"], 1) if has_src else 0.0
        if r["n_rest"]:
            f["F1_two_sided"] = float(r["two"] or 0) / max(r["n_markets"] or 0, 1)
            f["F3_trades_per_market"] = float(r["f3"]) if r["f3"] is not None else NAN
        out[r["entity_id"]] = {"flags": f, "turnover": float(r["turnover"] or 0.0), "rest": int(r["n_rest"])}
    return out


def maker_flags_from(trade_part: dict | None, pos: Cols) -> dict:
    """F1–F5 одного игрока: признаки сделок (maker_trade_flags) + F2, F4 по его позициям."""
    if not trade_part:
        return default_maker_flags()
    flags = dict(trade_part["flags"])
    if not trade_part["rest"]:
        return flags
    n = len(pos.get("cost_usd", ()))
    if n:
        sold = (pos["status"] == "closed") & np.isfinite(pos["closed_ts"])
        if sold.any():
            flags["F2_short_hold_h"] = float(np.median((pos["closed_ts"][sold] - pos["opened_ts"][sold]) / 3600))
    turnover = trade_part["turnover"]
    if turnover > 0 and n:
        rem = np.nan_to_num(pos["remaining_shares"], nan=0.0)
        flags["F4_net_turnover"] = float(np.nansum(rem * pos["avg_entry"])) / turnover
    return flags


def maker_flags(trades: pl.DataFrame, pos: pl.DataFrame, params: Params) -> dict:
    """F1–F5 для маркет-мейкеров и арбитражёров (сделки и позиции одного игрока)."""
    if trades.is_empty():
        return default_maker_flags()
    t = trades.with_columns(entity_id=pl.lit("_"))  # один игрок
    part = maker_trade_flags(t).get("_")
    return maker_flags_from(part, cols(pos) if not pos.is_empty() else {})


def is_maker(flags: dict, params: Params) -> bool:
    hits = 0
    hits += flags["F1_two_sided"] > params.maker_two_sided_share
    hits += bool(np.isfinite(flags["F2_short_hold_h"]) and flags["F2_short_hold_h"] < params.maker_median_hold_h)
    hits += flags["F3_trades_per_market"] > params.maker_trades_per_market
    hits += bool(np.isfinite(flags["F4_net_turnover"]) and flags["F4_net_turnover"] < params.maker_net_turnover)
    hits += flags["F5_merge_split"] > MERGE_SPLIT_SHARE
    return hits >= params.maker_flags_needed


def favorite_flags(closed: Cols, params: Params) -> dict:
    if not len(closed.get("cost_usd", ())):
        return {"fav_cost_share": 0.0, "fav_pnl_share": 0.0}
    fav = closed["avg_entry"] >= params.favorite_price
    cost, pnl = closed["cost_usd"], closed["pnl_usd"]
    total_cost = np.nansum(cost)
    win = pnl > 0
    pos_pnl = np.nansum(pnl[win])
    return {
        "fav_cost_share": float(np.nansum(cost[fav]) / total_cost) if total_cost else 0.0,
        "fav_pnl_share": float(np.nansum(pnl[fav & win]) / pos_pnl) if pos_pnl > 0 else 0.0,
    }


def is_favorite_grinder(f: dict, params: Params) -> bool:
    return f["fav_cost_share"] >= params.favorite_cost_share or f["fav_pnl_share"] > params.favorite_pnl_share


def insider_flags(pos: Cols, n_clusters: int, first_seen_ts: Any, market_volume: dict, params: Params) -> dict:
    """I1–I5 (7.3). Глубину стакана на момент ставки в истории обычно не восстановить,
    поэтому I3 считается по доле в объёме рынка."""
    f = {
        "I1_wallet_age_days": NAN,
        "I2_top_event_share": 0.0,
        "I3_size_vs_volume": 0.0,
        "I4_jump_share": 0.0,
        "I5_clusters": float(n_clusters),
    }
    cost = pos.get("cost_usd")
    if cost is None or not len(cost):
        return f
    big = int(np.nanargmax(cost))  # первая из позиций с наибольшими затратами
    if first_seen_ts is not None and np.isfinite(first_seen_ts):
        f["I1_wallet_age_days"] = float((pos["opened_ts"][big] - first_seen_ts) / 86400)
    keys = np.array([e if e is not None else m for e, m in zip(pos["event_id"], pos["market_id"])], dtype=object)
    total = np.nansum(cost)
    if total:
        ok = np.array([k is not None for k in keys])
        if ok.any():
            _, inv = np.unique(keys[ok].astype(str), return_inverse=True)
            f["I2_top_event_share"] = float(np.bincount(inv, weights=cost[ok]).max() / total)
        else:
            f["I2_top_event_share"] = NAN
    vol = market_volume.get(pos["market_id"][big]) or 0.0
    f["I3_size_vs_volume"] = float(cost[big] / vol) if vol > 0 else 0.0
    clv = pos.get("clv_24h")
    if clv is not None and np.isfinite(clv).any():
        c = clv[np.isfinite(clv)]
        f["I4_jump_share"] = float(np.mean(c >= params.insider_jump_pp))
    return f


def is_insider(f: dict, params: Params) -> bool:
    hits = 0
    hits += bool(np.isfinite(f["I1_wallet_age_days"]) and f["I1_wallet_age_days"] < params.insider_wallet_age_days)
    hits += f["I2_top_event_share"] > params.insider_single_event_share
    hits += f["I3_size_vs_volume"] > params.insider_big_vs_volume
    hits += f["I4_jump_share"] > params.insider_jump_share
    hits += f["I5_clusters"] < params.insider_min_clusters
    return hits >= params.insider_flags_needed


def hold_profile(closed: Cols) -> dict:
    """Доля удержания до разрешения и медианный срок удержания (8.4)."""
    if not len(closed.get("cost_usd", ())):
        return {"hold_share": NAN, "median_hold_h": NAN}
    hold = (closed["closed_ts"] - closed["opened_ts"]) / 3600
    return {
        "hold_share": float(np.nanmean(closed["held_to_resolution"])),
        "median_hold_h": float(np.nanmedian(hold)) if np.isfinite(hold).any() else NAN,
    }


def classify(
    trade_part: dict | None,
    pos: Cols,
    closed: Cols,
    n_clusters: int,
    first_seen_ts: Any,
    market_volume: dict,
    params: Params,
) -> tuple[str, dict]:
    mf = maker_flags_from(trade_part, pos)
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
