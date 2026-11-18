"""Бэктест копирования (ТЗ раздел 11) и walk-forward проверка отбора (6.9).

Главный вопрос: если бы я копировал отобранных игроков с задержкой δ,
что было бы после издержек и проскальзывания?

Правила честности (11.4):
  * отбор кошельков — только по данным до момента отбора (позиции и исходы
    пересобираются «на момент», разрешения после него скрыты);
  * цена входа — по снимку стакана на t+δ; если снимка нет — цена кита + штраф,
    а результат помечается как оптимистичная оценка;
  * базовые линии: топ по PnL без статистики и случайные игроки с похожим объёмом.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import numpy as np
import polars as pl

from .. import frames as fr
from ..config import Params
from ..db import DB
from ..positions import _build
from ..scoring.metrics import bucket_index, bucket_labels
from ..scoring.scorer import score_entities
from ..signals.orderbook import walk

log = logging.getLogger(__name__)
DAY = 86400


def _argsort(x: np.ndarray, descending: bool = False) -> np.ndarray:
    """Порядок сортировки как у прежнего движка: быстрая сортировка numpy, пропуски — в конце."""
    x = np.asarray(x, float)
    ok = np.flatnonzero(~np.isnan(x))
    order = ok[np.argsort(-x[ok] if descending else x[ok], kind="quicksort")]
    return np.concatenate([order, np.flatnonzero(np.isnan(x))]).astype(int)


class Frames:
    """Все данные в памяти для быстрых прогонов. Большие таблицы истории читает DuckDB."""

    def __init__(self, db: DB):
        self.trades = fr.read_history(
            db, "SELECT trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd FROM trades"
        )
        self.markets = fr.read(
            db, "SELECT market_id, event_id, category, status, resolved_ts, end_ts, volume_usd FROM markets"
        )
        self.tokens = fr.read(db, "SELECT token_id, market_id, payout FROM tokens")
        self.wallets = fr.read(db, "SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets")
        self.prices = fr.read_history(db, "SELECT token_id, ts, price FROM price_history")
        books = fr.read_history(db, "SELECT token_id, ts, asks FROM orderbook_snapshots")
        ent = dict(fr.read(db, "SELECT wallet, entity_id FROM entity_wallets").iter_rows())
        self.entity_of = lambda w: ent.get(w, w)
        self.trades = self.trades.with_columns(
            entity_id=pl.col("wallet").replace_strict(ent, default=pl.col("wallet"), return_dtype=pl.Utf8)
            if ent
            else pl.col("wallet")
        )
        self._price_idx = self._index(self.prices, "price")
        self._book_idx = self._index(books, "asks")

    @staticmethod
    def _index(df: pl.DataFrame, val: str) -> dict[str, tuple[np.ndarray, list]]:
        out: dict[str, tuple[np.ndarray, list]] = {}
        if df.is_empty():
            return out
        for (tok,), g in df.sort("ts", maintain_order=True).partition_by("token_id", as_dict=True).items():
            out[tok] = (g["ts"].to_numpy().astype(np.int64), g[val].to_list())
        return out

    def _markets_at(self, at: int) -> pl.DataFrame:
        hidden = pl.col("resolved_ts").is_null() | (pl.col("resolved_ts") > at)
        return self.markets.with_columns(resolved_ts=pl.when(hidden).then(None).otherwise(pl.col("resolved_ts")))

    def positions_at(self, at: int) -> pl.DataFrame:
        """Позиции так, как они выглядели в момент at (без будущих сделок и исходов)."""
        tr = self.trades.filter(pl.col("ts") <= at)
        mk = self._markets_at(at)
        tok = self.tokens.join(mk.select("market_id", "resolved_ts"), on="market_id", how="left", maintain_order="left")
        tok = tok.with_columns(payout=pl.when(pl.col("resolved_ts").is_null()).then(None).otherwise(pl.col("payout")))
        pos = _build(tr, mk, tok.select("token_id", "market_id", "payout"))
        if pos.is_empty():
            return pos
        ent = self.trades.select("wallet", "entity_id").unique("wallet")
        pos = pos.join(ent, on="wallet", how="left", maintain_order="left").with_columns(
            entity_id=pl.coalesce("entity_id", "wallet")
        )
        return pos.join(
            mk.select("market_id", "resolved_ts").unique("market_id"), on="market_id", how="left", maintain_order="left"
        )

    def price_at(self, token: str, ts: int, tol: int = 6 * 3600) -> float | None:
        g = self._price_idx.get(token)
        if g is None:
            return None
        i = int(np.searchsorted(g[0], ts, side="right")) - 1
        if i < 0 or ts - g[0][i] > tol:
            return None
        return float(g[1][i])

    def book_at(self, token: str, ts: int, tol: int = 120):
        g = self._book_idx.get(token)
        if g is None:
            return None
        i = int(np.searchsorted(g[0], ts, side="right")) - 1
        if i < 0 or ts - g[0][i] > tol:
            return None
        return [tuple(x) for x in json.loads(g[1][i])]


def select(frames: Frames, params: Params, at: int, seed: int = 7) -> tuple[pl.DataFrame, pl.DataFrame]:
    pos = frames.positions_at(at)
    if pos.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    return score_entities(
        pos,
        frames.trades.drop("entity_id"),
        frames.wallets,
        frames.markets,
        frames.prices.filter(pl.col("ts") <= at),
        params,
        at,
        seed,
    )


def simulate(
    frames: Frames,
    entities: list[str],
    cat_ok: dict[str, set] | None,
    start: int,
    end: int,
    delays: list[int],
    params: Params,
    size_usd: float = 20.0,
    penalty: float = 0.02,
    edge_by_entity: dict[str, float] | None = None,
    apply_drift_filter: bool = True,
    fee_bps: float = 0.0,
) -> list[dict]:
    """Копии первых покупок выбранных игроков в окне (start, end] для каждой задержки δ."""
    tr = frames.trades.filter(
        (pl.col("side") == "BUY")
        & (pl.col("ts") > start)
        & (pl.col("ts") <= end)
        & pl.col("entity_id").is_in(list(entities))
    )
    tr = tr[_argsort(tr["ts"].to_numpy())].unique(["entity_id", "token_id"], keep="first", maintain_order=True)
    mk = {r["market_id"]: r for r in frames.markets.iter_rows(named=True)}
    payout = dict(frames.tokens.select("token_id", "payout").iter_rows())
    tok_market = dict(frames.tokens.select("token_id", "market_id").iter_rows())
    rows: list[dict] = []
    for t in tr.iter_rows(named=True):
        tok_id, ent, price, t_ts = t["token_id"], t["entity_id"], t["price"], t["ts"]
        mid = tok_market.get(tok_id, t["market_id"])
        m = mk.get(mid)
        cat = m["category"] if m is not None else "other"
        if cat_ok is not None and cat not in cat_ok.get(ent, set()):
            continue
        for d in delays:
            ts = int(t_ts + d)
            asks = frames.book_at(tok_id, ts)
            if asks:
                fill = walk(asks, usd=size_usd)
                entry, optimistic = fill.avg_price, False
            else:
                px = frames.price_at(tok_id, ts)
                entry = max(px if px is not None else price, price) + penalty
                optimistic = True
            entry = float(min(entry, 0.999))
            e_cat = (edge_by_entity or {}).get(ent, 0.05)
            if apply_drift_filter and entry - price > max(params.drift_abs, params.drift_edge_frac * e_cat) + (
                penalty if optimistic else 0
            ):
                rows.append(
                    {
                        "delay": d,
                        "entity_id": ent,
                        "token_id": tok_id,
                        "category": cat,
                        "ts": t_ts,
                        "whale_price": price,
                        "entry": entry,
                        "status": "filtered",
                        "optimistic": optimistic,
                    }
                )
                continue
            y = payout.get(tok_id)
            res_ts = m["resolved_ts"] if m is not None else None
            if y is None or (isinstance(y, float) and np.isnan(y)):
                status, pnl = "open", float("nan")
            else:
                status = "closed"
                pnl = size_usd * (float(y) / entry - 1) - size_usd * fee_bps / 1e4
            rows.append(
                {
                    "delay": d,
                    "entity_id": ent,
                    "token_id": tok_id,
                    "category": cat,
                    "ts": t_ts,
                    "resolved_ts": res_ts,
                    "whale_price": price,
                    "entry": entry,
                    "stake": size_usd,
                    "payout": y,
                    "pnl": pnl,
                    "status": status,
                    "optimistic": optimistic,
                }
            )
    return rows


def summarize(sim: list[dict], buckets: tuple, rng: Any = None) -> dict:
    """ROI по задержкам, корзинам, категориям, кривая капитала и просадка (11.5)."""
    rng = rng or np.random.default_rng(0)
    out: dict = {}
    if not sim:
        return out
    labels = bucket_labels(buckets)
    for d in sorted({r["delay"] for r in sim}):
        g = [r for r in sim if r["delay"] == d]
        n_filtered = sum(r["status"] == "filtered" for r in g)
        c = [r for r in g if r["status"] == "closed"]
        if not c:
            out[int(d)] = {"n": 0, "filtered": int(n_filtered)}
            continue
        res = np.array([np.nan if r.get("resolved_ts") is None else r["resolved_ts"] for r in c], float)
        c = [c[i] for i in _argsort(res)]
        pnl = np.array([r["pnl"] for r in c], float)
        stake = np.array([r["stake"] for r in c], float)
        entry = np.array([r["entry"] for r in c], float)
        roi = pnl.sum() / stake.sum()
        boots = []
        for _ in range(1000):
            i = rng.integers(0, len(c), len(c))
            boots.append(pnl[i].sum() / stake[i].sum())
        equity = np.cumsum(pnl)
        peak = np.maximum.accumulate(np.concatenate([[0], equity]))[1:]
        bi = bucket_index(entry, buckets)
        by_bucket = {
            labels[k]: {
                "n": int((bi == k).sum()),
                "roi": float(pnl[bi == k].sum() / stake[bi == k].sum()),
                "win_rate": float((pnl[bi == k] > 0).mean()),
                "avg_entry": float(entry[bi == k].mean()),
            }
            for k in np.unique(bi[bi >= 0])
        }
        cats = np.array([r["category"] for r in c], dtype=object)
        by_cat = {
            str(k): {"n": int((cats == k).sum()), "roi": float(pnl[cats == k].sum() / stake[cats == k].sum())}
            for k in sorted({x for x in cats if x is not None})
        }
        out[int(d)] = {
            "n": len(c),
            "open": int(sum(r["status"] == "open" for r in g)),
            "filtered": int(n_filtered),
            "roi": float(roi),
            "roi_ci90": [float(np.percentile(boots, 5)), float(np.percentile(boots, 95))],
            "win_rate": float((pnl > 0).mean()),
            "avg_entry": float(entry.mean()),
            "pnl_usd": float(pnl.sum()),
            "max_drawdown_usd": float((peak - equity).max()),
            "optimistic_share": float(np.mean([bool(r["optimistic"]) for r in c])),
            "by_bucket": by_bucket,
            "by_category": by_cat,
        }
    return out


def _approved_cats(cats: pl.DataFrame, ent: list[str]) -> dict[str, set]:
    if cats.is_empty():
        return {}
    ok = cats.filter(pl.col("status") == "approved")
    return {e: set(ok.filter(pl.col("entity_id") == e)["category"].to_list()) for e in ent}


def _eligible(scores: pl.DataFrame) -> pl.DataFrame:
    return scores.filter((pl.col("class") == "normal") & (pl.col("n_clusters") >= 10))


def run_backtest(
    db: DB,
    params: Params,
    select_at: int,
    test_end: int,
    delays=(5, 30, 120, 600),
    size_usd: float = 20.0,
    penalty: float = 0.02,
    n_baseline: int | None = None,
    seed: int = 7,
) -> dict:
    frames = Frames(db)
    scores, cats = select(frames, params, select_at, seed)
    if scores.is_empty():
        return {"error": "Нет данных для отбора"}
    ent = scores.filter(pl.col("status") == "approved")["entity_id"].to_list()
    cat_ok = _approved_cats(cats, ent)
    edge = dict(zip(scores["entity_id"].to_list(), scores["edge_shrunk"].to_list()))
    res = {
        "select_at": select_at,
        "test_end": test_end,
        "selected": [str(e) for e in ent],
        "params_version": params.version(),
        "delays": list(delays),
    }
    res["strategy"] = summarize(
        simulate(frames, ent, cat_ok, select_at, test_end, list(delays), params, size_usd, penalty, edge),
        params.price_buckets,
    )
    k = n_baseline or max(len(ent), 5)
    eligible = _eligible(scores)
    el_ids = eligible["entity_id"].to_numpy()
    pnl_est = (eligible["edge_raw"] * eligible["total_cost"]).to_numpy().astype(float)
    top = [str(x) for x in el_ids[_argsort(pnl_est, descending=True)][:k]]
    rng = np.random.default_rng(seed)
    rnd = [str(x) for x in rng.choice(el_ids, size=min(k, len(el_ids)), replace=False)] if len(el_ids) else []
    res["baseline_top_pnl"] = summarize(
        simulate(frames, top, None, select_at, test_end, list(delays), params, size_usd, penalty, edge),
        params.price_buckets,
    )
    res["baseline_random"] = summarize(
        simulate(frames, rnd, None, select_at, test_end, list(delays), params, size_usd, penalty, edge),
        params.price_buckets,
    )
    d30 = res["strategy"].get(30, {})
    res["verdict"] = {
        "roi_at_30s": d30.get("roi"),
        "roi_ci90_at_30s": d30.get("roi_ci90"),
        "continue": bool(d30.get("n", 0) >= 20 and (d30.get("roi_ci90") or [-1])[0] > 0),
        "note": (
            "Критерий «стоп» ТЗ: если ROI при задержке 30 с не выше нуля после издержек — проект "
            "останавливается или меняется подход."
        ),
    }
    return res


def walk_forward(
    db: DB,
    params: Params,
    first_select: int,
    last_select: int,
    step_days: int = 30,
    test_days: int = 30,
    delay: int = 30,
    size_usd: float = 20.0,
    seed: int = 7,
) -> dict:
    """Отбор на прошлом окне, проверка на следующем, сдвиг (6.9)."""
    frames = Frames(db)
    folds = []
    at = first_select
    rng = np.random.default_rng(seed)
    while at <= last_select:
        scores, cats = select(frames, params, at, seed)
        if scores.is_empty():
            at += step_days * DAY
            continue
        ent = scores.filter(pl.col("status") == "approved")["entity_id"].to_list()
        cat_ok = _approved_cats(cats, ent)
        edge = dict(zip(scores["entity_id"].to_list(), scores["edge_shrunk"].to_list()))
        end = at + test_days * DAY
        sel = simulate(frames, ent, cat_ok, at, end, [delay], params, size_usd, edge_by_entity=edge)
        eligible = _eligible(scores)["entity_id"].to_numpy()
        rnd = (
            [str(x) for x in rng.choice(eligible, size=min(max(len(ent), 5), len(eligible)), replace=False)]
            if len(eligible)
            else []
        )
        base = simulate(frames, rnd, None, at, end, [delay], params, size_usd, edge_by_entity=edge)

        def roi(rows):
            c = [r for r in rows if r["status"] == "closed"]
            if not c:
                return float("nan"), 0
            return float(sum(r["pnl"] for r in c) / sum(r["stake"] for r in c)), len(c)

        r_sel, n_sel = roi(sel)
        r_rnd, n_rnd = roi(base)
        folds.append(
            {
                "select_at": at,
                "selected": len(ent),
                "roi_selected": r_sel,
                "n_selected": n_sel,
                "roi_random": r_rnd,
                "n_random": n_rnd,
            }
        )
        at += step_days * DAY
    ok = [f for f in folds if np.isfinite(f["roi_selected"]) and np.isfinite(f["roi_random"])]
    m_sel = float(np.mean([f["roi_selected"] for f in ok])) if ok else None
    m_rnd = float(np.mean([f["roi_random"] for f in ok])) if ok else None
    return {
        "folds": folds,
        "mean_roi_selected": m_sel,
        "mean_roi_random": m_rnd,
        "selection_works": bool(ok and m_sel is not None and m_rnd is not None and m_sel > 0 and m_sel > m_rnd),
    }


def quick_params(params: Params) -> Params:
    """Облегчённые параметры для многократных прогонов (меньше пересборок)."""
    return replace(params, bootstrap_b=min(params.bootstrap_b, 1000), null_sims=min(params.null_sims, 2000))
