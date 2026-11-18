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

import numpy as np
import pandas as pd

from ..config import Params
from ..db import DB
from ..positions import _build
from ..scoring.scorer import score_entities
from ..signals.orderbook import walk

log = logging.getLogger(__name__)
DAY = 86400


class Frames:
    """Все данные в памяти для быстрых прогонов."""

    def __init__(self, db: DB):
        q = db.query
        self.trades = pd.DataFrame(
            q("SELECT trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd FROM trades")
        )
        self.markets = pd.DataFrame(
            q("SELECT market_id, event_id, category, status, resolved_ts, end_ts, volume_usd FROM markets")
        )
        self.tokens = pd.DataFrame(q("SELECT token_id, market_id, payout FROM tokens"))
        self.wallets = pd.DataFrame(q("SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets"))
        self.prices = pd.DataFrame(q("SELECT token_id, ts, price FROM price_history"))
        if self.prices.empty:
            self.prices = pd.DataFrame(columns=["token_id", "ts", "price"])
        self.books = pd.DataFrame(q("SELECT token_id, ts, asks FROM orderbook_snapshots"))
        ent = {r["wallet"]: r["entity_id"] for r in q("SELECT wallet, entity_id FROM entity_wallets")}
        self.entity_of = lambda w: ent.get(w, w)
        self.trades["entity_id"] = self.trades["wallet"].map(self.entity_of)
        self._price_idx = (
            {t: g.sort_values("ts") for t, g in self.prices.groupby("token_id")} if len(self.prices) else {}
        )
        self._book_idx = {t: g.sort_values("ts") for t, g in self.books.groupby("token_id")} if len(self.books) else {}

    def positions_at(self, at: int) -> pd.DataFrame:
        """Позиции так, как они выглядели в момент at (без будущих сделок и исходов)."""
        tr = self.trades[self.trades["ts"] <= at]
        mk = self.markets.copy()
        hidden = mk["resolved_ts"].isna() | (mk["resolved_ts"] > at)
        mk.loc[hidden, "resolved_ts"] = np.nan
        tok = self.tokens.merge(mk[["market_id", "resolved_ts"]], on="market_id", how="left")
        tok.loc[tok["resolved_ts"].isna(), "payout"] = np.nan
        pos = _build(tr, mk, tok[["token_id", "market_id", "payout"]])
        if pos.empty:
            return pos
        pos["entity_id"] = pos["wallet"].map(self.entity_of)
        return pos.merge(mk[["market_id", "resolved_ts"]], on="market_id", how="left")

    def price_at(self, token: str, ts: int, tol: int = 6 * 3600) -> float | None:
        g = self._price_idx.get(token)
        if g is None:
            return None
        i = np.searchsorted(g["ts"].to_numpy(), ts, side="right") - 1
        if i < 0 or ts - g["ts"].iloc[i] > tol:
            return None
        return float(g["price"].iloc[i])

    def book_at(self, token: str, ts: int, tol: int = 120):
        g = self._book_idx.get(token)
        if g is None:
            return None
        i = np.searchsorted(g["ts"].to_numpy(), ts, side="right") - 1
        if i < 0 or ts - g["ts"].iloc[i] > tol:
            return None
        return [tuple(x) for x in json.loads(g["asks"].iloc[i])]


def select(frames: Frames, params: Params, at: int, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    pos = frames.positions_at(at)
    if pos.empty:
        return pd.DataFrame(), pd.DataFrame()
    return score_entities(
        pos,
        frames.trades.drop(columns=["entity_id"]),
        frames.wallets,
        frames.markets,
        frames.prices[frames.prices["ts"] <= at],
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
) -> pd.DataFrame:
    """Копии первых покупок выбранных игроков в окне (start, end] для каждой задержки δ."""
    tr = frames.trades
    tr = tr[(tr["side"] == "BUY") & (tr["ts"] > start) & (tr["ts"] <= end) & tr["entity_id"].isin(entities)]
    tr = tr.sort_values("ts").drop_duplicates(["entity_id", "token_id"])
    mk = frames.markets.set_index("market_id")
    payout = frames.tokens.set_index("token_id")["payout"].to_dict()
    tok_market = frames.tokens.set_index("token_id")["market_id"].to_dict()
    rows = []
    for t in tr.itertuples():
        mid = tok_market.get(t.token_id, t.market_id)
        cat = mk["category"].get(mid, "other") if mid in mk.index else "other"
        if cat_ok is not None and cat not in cat_ok.get(t.entity_id, set()):
            continue
        for d in delays:
            ts = int(t.ts + d)
            asks = frames.book_at(t.token_id, ts)
            if asks:
                fill = walk(asks, usd=size_usd)
                entry, optimistic = fill.avg_price, False
            else:
                px = frames.price_at(t.token_id, ts)
                entry = max(px if px is not None else t.price, t.price) + penalty
                optimistic = True
            entry = float(min(entry, 0.999))
            e_cat = (edge_by_entity or {}).get(t.entity_id, 0.05)
            if apply_drift_filter and entry - t.price > max(params.drift_abs, params.drift_edge_frac * e_cat) + (
                penalty if optimistic else 0
            ):
                rows.append(
                    {
                        "delay": d,
                        "entity_id": t.entity_id,
                        "token_id": t.token_id,
                        "category": cat,
                        "ts": t.ts,
                        "whale_price": t.price,
                        "entry": entry,
                        "status": "filtered",
                        "optimistic": optimistic,
                    }
                )
                continue
            y = payout.get(t.token_id)
            res_ts = mk["resolved_ts"].get(mid) if mid in mk.index else None
            if y is None or (isinstance(y, float) and np.isnan(y)):
                status, pnl = "open", np.nan
            else:
                status = "closed"
                pnl = size_usd * (float(y) / entry - 1) - size_usd * fee_bps / 1e4
            rows.append(
                {
                    "delay": d,
                    "entity_id": t.entity_id,
                    "token_id": t.token_id,
                    "category": cat,
                    "ts": t.ts,
                    "resolved_ts": res_ts,
                    "whale_price": t.price,
                    "entry": entry,
                    "stake": size_usd,
                    "payout": y,
                    "pnl": pnl,
                    "status": status,
                    "optimistic": optimistic,
                }
            )
    return pd.DataFrame(rows)


def summarize(sim: pd.DataFrame, buckets: tuple, rng=None) -> dict:
    """ROI по задержкам, корзинам, категориям, кривая капитала и просадка (11.5)."""
    rng = rng or np.random.default_rng(0)
    out = {}
    if sim.empty:
        return out
    for d, g in sim.groupby("delay"):
        c = g[g["status"] == "closed"].sort_values("resolved_ts")
        if c.empty:
            out[int(d)] = {"n": 0, "filtered": int((g["status"] == "filtered").sum())}
            continue
        roi = c["pnl"].sum() / c["stake"].sum()
        boots = []
        pnl, stake = c["pnl"].to_numpy(), c["stake"].to_numpy()
        for _ in range(1000):
            i = rng.integers(0, len(c), len(c))
            boots.append(pnl[i].sum() / stake[i].sum())
        equity = c["pnl"].cumsum().to_numpy()
        peak = np.maximum.accumulate(np.concatenate([[0], equity]))[1:]
        labels = [f"{buckets[i]:.1f}-{min(buckets[i + 1], 1):.1f}" for i in range(len(buckets) - 1)]
        b = pd.cut(c["entry"], bins=list(buckets), labels=labels, right=False)
        by_bucket = {
            str(k): {
                "n": int(len(v)),
                "roi": float(v["pnl"].sum() / v["stake"].sum()),
                "win_rate": float((v["pnl"] > 0).mean()),
                "avg_entry": float(v["entry"].mean()),
            }
            for k, v in c.groupby(b, observed=True)
        }
        by_cat = {
            str(k): {"n": int(len(v)), "roi": float(v["pnl"].sum() / v["stake"].sum())}
            for k, v in c.groupby("category")
        }
        out[int(d)] = {
            "n": int(len(c)),
            "open": int((g["status"] == "open").sum()),
            "filtered": int((g["status"] == "filtered").sum()),
            "roi": float(roi),
            "roi_ci90": [float(np.percentile(boots, 5)), float(np.percentile(boots, 95))],
            "win_rate": float((c["pnl"] > 0).mean()),
            "avg_entry": float(c["entry"].mean()),
            "pnl_usd": float(c["pnl"].sum()),
            "max_drawdown_usd": float((peak - equity).max()),
            "optimistic_share": float(c["optimistic"].mean()),
            "by_bucket": by_bucket,
            "by_category": by_cat,
        }
    return out


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
    if scores.empty:
        return {"error": "Нет данных для отбора"}
    approved = scores[scores["status"] == "approved"]
    ent = list(approved["entity_id"])
    cat_ok = (
        {e: set(cats[(cats["entity_id"] == e) & (cats["status"] == "approved")]["category"]) for e in ent}
        if not cats.empty
        else {}
    )
    edge = dict(zip(scores["entity_id"], scores["edge_shrunk"]))
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
    eligible = scores[(scores["class"] == "normal") & (scores["n_clusters"] >= 10)]
    top = list(
        eligible.assign(pnl=eligible["edge_raw"] * eligible["total_cost"])
        .sort_values("pnl", ascending=False)
        .head(k)["entity_id"]
    )
    rng = np.random.default_rng(seed)
    rnd = (
        list(rng.choice(eligible["entity_id"].to_numpy(), size=min(k, len(eligible)), replace=False))
        if len(eligible)
        else []
    )
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
        if scores.empty:
            at += step_days * DAY
            continue
        ent = list(scores[scores["status"] == "approved"]["entity_id"])
        cat_ok = {e: set(cats[(cats["entity_id"] == e) & (cats["status"] == "approved")]["category"]) for e in ent}
        edge = dict(zip(scores["entity_id"], scores["edge_shrunk"]))
        end = at + test_days * DAY
        sel = simulate(frames, ent, cat_ok, at, end, [delay], params, size_usd, edge_by_entity=edge)
        eligible = scores[(scores["class"] == "normal") & (scores["n_clusters"] >= 10)]["entity_id"].to_numpy()
        rnd = (
            list(rng.choice(eligible, size=min(max(len(ent), 5), len(eligible)), replace=False))
            if len(eligible)
            else []
        )
        base = simulate(frames, rnd, None, at, end, [delay], params, size_usd, edge_by_entity=edge)

        def roi(df):
            c = df[df["status"] == "closed"] if not df.empty else df
            return (float(c["pnl"].sum() / c["stake"].sum()), int(len(c))) if len(c) else (float("nan"), 0)

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
    df = pd.DataFrame(folds)
    ok = df.dropna(subset=["roi_selected", "roi_random"]) if not df.empty else df
    return {
        "folds": folds,
        "mean_roi_selected": float(ok["roi_selected"].mean()) if len(ok) else None,
        "mean_roi_random": float(ok["roi_random"].mean()) if len(ok) else None,
        "selection_works": bool(
            len(ok) and ok["roi_selected"].mean() > 0 and ok["roi_selected"].mean() > ok["roi_random"].mean()
        ),
    }


def quick_params(params: Params) -> Params:
    """Облегчённые параметры для многократных прогонов (меньше пересборок)."""
    return replace(params, bootstrap_b=min(params.bootstrap_b, 1000), null_sims=min(params.null_sims, 2000))
