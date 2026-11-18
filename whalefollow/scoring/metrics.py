"""Метрики позиций и кластеров (ТЗ 6.2–6.3). Движок — polars."""

from __future__ import annotations

import numpy as np
import polars as pl

from .. import frames
from ..config import Params

CLUSTER_COLS = [
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


def add_clv(pos: pl.DataFrame, prices: pl.DataFrame, params: Params, tolerance_s: int = 6 * 3600) -> pl.DataFrame:
    """CLV(Δ) = P(t_open + Δ) − p_entry.

    Если рынок разрешается раньше, берётся цена за cutoff (1 ч) до разрешения;
    позиции, открытые позже этого момента, в CLV не участвуют.
    """
    cutoff = params.clv_cutoff_before_resolution_s
    names = {params.clv_windows_s[0]: "clv_1h", params.clv_windows_s[1]: "clv_24h"}
    pos = pos.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in names.values()])
    if pos.is_empty() or prices is None or prices.is_empty():
        return pos
    pos = pos.with_row_index("_i")
    if "resolved_ts" not in pos.columns:
        pos = pos.with_columns(resolved_ts=pl.lit(None, dtype=pl.Int64))
    pr = prices.select(
        pl.col("token_id").cast(pl.Utf8), pl.col("ts").cast(pl.Int64), pl.col("price").cast(pl.Float64)
    ).sort("ts")
    opened = pl.col("opened_ts").cast(pl.Int64)
    limit = pl.col("resolved_ts").cast(pl.Int64) - cutoff
    for window, col in names.items():
        target = pl.when(limit.is_not_null()).then(pl.min_horizontal(opened + window, limit)).otherwise(opened + window)
        valid = ~(limit.is_not_null() & (opened > limit))
        q = pos.filter(valid & opened.is_not_null()).select("_i", "token_id", ts=target.cast(pl.Int64)).sort("ts")
        if q.is_empty():
            continue
        m = q.join_asof(pr, on="ts", by="token_id", strategy="backward", tolerance=tolerance_s, check_sortedness=False)
        pos = (
            pos.drop(col)
            .join(m.select("_i", pl.col("price").alias("_p")), on="_i", how="left", maintain_order="left")
            .with_columns((pl.col("_p") - pl.col("avg_entry")).alias(col))
            .drop("_p")
        )
    return pos.drop("_i")


def closed_positions(pos: pl.DataFrame, now: int | None = None) -> pl.DataFrame:
    """Закрытые позиции с известным R (на момент now — без заглядывания в будущее)."""
    ev = pl.col("exit_value_per_share")
    m = pl.col("status").is_in(["resolved", "closed"]) & ev.is_not_null() & ~ev.is_nan()
    if now is not None:
        m = m & pl.col("closed_ts").is_not_null() & (pl.col("closed_ts") <= now)
    return pos.filter(m).with_columns(
        edge=ev - pl.col("avg_entry"),
        roi=ev / pl.col("avg_entry").clip(lower_bound=1e-6) - 1,
    )


def cluster_table(closed: pl.DataFrame) -> pl.DataFrame:
    """Агрегация в кластеры событий: edge_c — взвешенный по затратам, w_c = sqrt(cost_c).

    Для теста на везение берётся представитель кластера — позиция с наибольшими затратами
    (при равных затратах — первая по порядку).
    """
    if closed.is_empty():
        return frames.empty(CLUSTER_COLS).with_columns(
            [
                pl.col(c).cast(pl.Float64)
                for c in ("edge_c", "cost_c", "w_c", "p_rep", "r_rep", "clv_1h", "clv_24h", "pnl")
            ]
        )
    df = closed
    for col in ("clv_1h", "clv_24h"):
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
    keys = ["entity_id", "cluster"]
    df = df.with_columns(
        cluster=pl.coalesce("event_id", "market_id", "token_id"),
        e_x_c=pl.col("edge") * pl.col("cost_usd"),
        **{f"{c}_x": pl.col(c) * pl.col("cost_usd") for c in ("clv_1h", "clv_24h")},
        **{
            f"{c}_c": pl.when(pl.col(c).is_not_null() & ~pl.col(c).is_nan()).then(pl.col("cost_usd")).otherwise(0.0)
            for c in ("clv_1h", "clv_24h")
        },
    ).with_columns(_rank=pl.col("cost_usd").rank("ordinal", descending=True).over(keys))
    rep = df.filter(pl.col("_rank") == 1).select(
        *keys, pl.col("avg_entry").alias("p_rep"), pl.col("exit_value_per_share").alias("r_rep"), "category"
    )
    agg = (
        df.group_by(keys)
        .agg(
            cost_c=pl.col("cost_usd").sum(),
            e_x_c=pl.col("e_x_c").sum(),
            ts=pl.col("opened_ts").min(),
            pnl=pl.col("pnl_usd").sum(),
            clv_1h_x=pl.col("clv_1h_x").fill_nan(None).sum(),
            clv_1h_c=pl.col("clv_1h_c").sum(),
            clv_24h_x=pl.col("clv_24h_x").fill_nan(None).sum(),
            clv_24h_c=pl.col("clv_24h_c").sum(),
        )
        .sort(keys)
    )
    agg = agg.with_columns(
        edge_c=pl.col("e_x_c") / pl.col("cost_c").clip(lower_bound=1e-9),
        w_c=pl.col("cost_c").clip(lower_bound=0).sqrt(),
        clv_1h=pl.when(pl.col("clv_1h_c") > 0).then(pl.col("clv_1h_x") / pl.col("clv_1h_c").clip(lower_bound=1e-9)),
        clv_24h=pl.when(pl.col("clv_24h_c") > 0).then(pl.col("clv_24h_x") / pl.col("clv_24h_c").clip(lower_bound=1e-9)),
    ).join(rep, on=keys, how="left", maintain_order="left")
    return agg.select(CLUSTER_COLS)


def bucket_labels(buckets: tuple) -> list[str]:
    return [f"{buckets[i]:.1f}-{min(buckets[i + 1], 1):.1f}" for i in range(len(buckets) - 1)]


def bucket_index(x: np.ndarray, buckets: tuple) -> np.ndarray:
    """Номер корзины цены [b_i, b_i+1); −1 — вне корзин (как pd.cut(right=False))."""
    edges = np.asarray(buckets, float)
    x = np.asarray(x, float)
    i = np.searchsorted(edges, x, side="right") - 1
    i[(i >= len(edges) - 1) | ~np.isfinite(x)] = -1
    return i


def bucket_edges(closed: dict | pl.DataFrame, buckets: tuple) -> dict:
    """Edge и доля затрат по корзинам цены входа (6.6). closed — таблица или словарь массивов numpy."""
    get = (lambda c: closed[c].to_numpy().astype(float)) if isinstance(closed, pl.DataFrame) else closed.__getitem__
    entry = np.asarray(get("avg_entry"), float)
    out: dict = {}
    if len(entry) == 0:
        return out
    cost, edge, ev = (np.asarray(get(c), float) for c in ("cost_usd", "edge", "exit_value_per_share"))
    labels = bucket_labels(buckets)
    idx = bucket_index(entry, buckets)
    total = cost.sum()
    for k in np.unique(idx[idx >= 0]):
        m = idx == k
        c = cost[m].sum()
        out[labels[k]] = {
            "n": int(m.sum()),
            "cost_share": float(c / total) if total else 0.0,
            "edge": float((edge[m] * cost[m]).sum() / c) if c else 0.0,
            "win_rate": float((ev[m] > entry[m]).mean()),
            "avg_entry": float(entry[m].mean()),
        }
    return out
