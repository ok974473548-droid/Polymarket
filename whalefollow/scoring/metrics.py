"""Метрики позиций и кластеров (ТЗ 6.2–6.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Params


def add_clv(pos: pd.DataFrame, prices: pd.DataFrame, params: Params, tolerance_s: int = 6 * 3600) -> pd.DataFrame:
    """CLV(Δ) = P(t_open + Δ) − p_entry.

    Если рынок разрешается раньше, берётся цена за cutoff (1 ч) до разрешения;
    позиции, открытые позже этого момента, в CLV не участвуют.
    """
    pos = pos.copy()
    cutoff = params.clv_cutoff_before_resolution_s
    names = {params.clv_windows_s[0]: "clv_1h", params.clv_windows_s[1]: "clv_24h"}
    for col in names.values():
        pos[col] = np.nan
    if pos.empty or prices is None or prices.empty:
        return pos
    prices = prices.sort_values("ts")
    resolved_ts = pd.to_numeric(pos.get("resolved_ts", pd.Series(np.nan, index=pos.index)), errors="coerce")
    for window, col in names.items():
        target = pos["opened_ts"].astype(float) + window
        limit = resolved_ts - cutoff
        target = np.where(np.isfinite(limit), np.minimum(target, limit), target)
        valid = ~(np.isfinite(limit) & (pos["opened_ts"].astype(float) > limit))
        q = pd.DataFrame({"idx": pos.index, "token_id": pos["token_id"].values, "ts": np.asarray(target, float)}).loc[
            valid.values
        ]
        if q.empty:
            continue
        q["ts"] = q["ts"].astype("int64")
        q = q.sort_values("ts")
        merged = pd.merge_asof(
            q,
            prices[["token_id", "ts", "price"]].astype({"ts": "int64"}),
            on="ts",
            by="token_id",
            direction="backward",
            tolerance=tolerance_s,
        )
        merged = merged.set_index("idx")
        pos.loc[merged.index, col] = merged["price"].values - pos.loc[merged.index, "avg_entry"].values
    return pos


def closed_positions(pos: pd.DataFrame, now: int | None = None) -> pd.DataFrame:
    """Закрытые позиции с известным R (на момент now — без заглядывания в будущее)."""
    m = pos["status"].isin(["resolved", "closed"]) & pos["exit_value_per_share"].notna()
    if now is not None:
        m &= pos["closed_ts"].fillna(np.inf) <= now
    out = pos[m].copy()
    out["edge"] = out["exit_value_per_share"] - out["avg_entry"]
    out["roi"] = out["exit_value_per_share"] / out["avg_entry"].clip(lower=1e-6) - 1
    return out


def cluster_table(closed: pd.DataFrame) -> pd.DataFrame:
    """Агрегация в кластеры событий: edge_c — взвешенный по затратам, w_c = sqrt(cost_c).

    Для теста на везение берётся представитель кластера — позиция с наибольшими затратами.
    """
    if closed.empty:
        return pd.DataFrame(
            columns=[
                "entity_id",
                "cluster",
                "edge_c",
                "cost_c",
                "w_c",
                "p_rep",
                "r_rep",
                "clv_1h",
                "clv_24h",
                "ts",
                "category",
                "pnl",
            ]
        )
    df = closed.copy()
    df["cluster"] = df["event_id"].fillna(df["market_id"]).fillna(df["token_id"])
    df["e_x_c"] = df["edge"] * df["cost_usd"]
    for col in ("clv_1h", "clv_24h"):
        if col not in df:
            df[col] = np.nan
        df[col + "_x"] = df[col] * df["cost_usd"]
        df[col + "_c"] = np.where(df[col].notna(), df["cost_usd"], 0.0)
    df["_rank"] = df.groupby(["entity_id", "cluster"])["cost_usd"].rank(method="first", ascending=False)
    rep = df[df["_rank"] == 1][["entity_id", "cluster", "avg_entry", "exit_value_per_share", "category"]]
    agg = (
        df.groupby(["entity_id", "cluster"])
        .agg(
            cost_c=("cost_usd", "sum"),
            e_x_c=("e_x_c", "sum"),
            ts=("opened_ts", "min"),
            pnl=("pnl_usd", "sum"),
            clv_1h_x=("clv_1h_x", "sum"),
            clv_1h_c=("clv_1h_c", "sum"),
            clv_24h_x=("clv_24h_x", "sum"),
            clv_24h_c=("clv_24h_c", "sum"),
        )
        .reset_index()
    )
    agg["edge_c"] = agg["e_x_c"] / agg["cost_c"].clip(lower=1e-9)
    agg["w_c"] = np.sqrt(agg["cost_c"].clip(lower=0))
    agg["clv_1h"] = np.where(agg["clv_1h_c"] > 0, agg["clv_1h_x"] / agg["clv_1h_c"].clip(lower=1e-9), np.nan)
    agg["clv_24h"] = np.where(agg["clv_24h_c"] > 0, agg["clv_24h_x"] / agg["clv_24h_c"].clip(lower=1e-9), np.nan)
    agg = agg.merge(
        rep.rename(columns={"avg_entry": "p_rep", "exit_value_per_share": "r_rep"}),
        on=["entity_id", "cluster"],
        how="left",
    )
    return agg[
        [
            "entity_id",
            "cluster",
            "edge_c",
            "cost_c",
            "w_c",
            "p_rep",
            "r_rep",
            "clv_1h",
            "clv_24h",
            "ts",
            "category",
            "pnl",
        ]
    ]


def bucket_edges(closed: pd.DataFrame, buckets: tuple) -> dict:
    """Edge и доля затрат по корзинам цены входа (6.6)."""
    out: dict = {}
    if closed.empty:
        return out
    labels = [f"{buckets[i]:.1f}-{min(buckets[i + 1], 1):.1f}" for i in range(len(buckets) - 1)]
    b = pd.cut(closed["avg_entry"], bins=list(buckets), labels=labels, right=False, include_lowest=True)
    total = closed["cost_usd"].sum()
    for lab, g in closed.groupby(b, observed=True):
        out[str(lab)] = {
            "n": int(len(g)),
            "cost_share": float(g["cost_usd"].sum() / total) if total else 0.0,
            "edge": float((g["edge"] * g["cost_usd"]).sum() / g["cost_usd"].sum()) if g["cost_usd"].sum() else 0.0,
            "win_rate": float((g["exit_value_per_share"] > g["avg_entry"]).mean()),
            "avg_entry": float(g["avg_entry"].mean()),
        }
    return out
