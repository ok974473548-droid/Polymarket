"""Скоринг игроков (ТЗ 6.3–6.9) и формирование живого списка китов.

Вход — позиции (с entity_id), сделки, кошельки, рынки, история цен.
Все расчёты делаются «на момент now»: в бэктесте now — конец окна отбора,
и данные после него не используются.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from ..config import Params
from ..db import DB, now_ts
from . import classify as cls
from .metrics import add_clv, bucket_edges, closed_positions, cluster_table
from .stats import bh_qvalues, bootstrap_wmean, null_pvalue, shrink, wmean, zscore

log = logging.getLogger(__name__)
DAY = 86400


def _entity_core(e: str, cl: pd.DataFrame, closed: pd.DataFrame, pos_e: pd.DataFrame, now: int,
                 params: Params, rng: np.random.Generator, heavy: bool = True) -> dict:
    """heavy=False — без бутстрепа: у игрока слишком мало событий, отбор он всё равно не пройдёт."""
    x, w = cl["edge_c"].to_numpy(float), cl["w_c"].to_numpy(float)
    n = len(cl)
    e_raw = wmean(x, w) if n else float("nan")
    e_shr = shrink(e_raw, n, params.shrink_k)
    lo, hi = bootstrap_wmean(x, w, params.bootstrap_b, rng, k=params.shrink_k) if n and heavy else (np.nan, np.nan)
    clv24 = cl["clv_24h"].to_numpy(float)
    clv_lo, _ = bootstrap_wmean(clv24, w, max(500, params.bootstrap_b // 5), rng) \
        if heavy and np.isfinite(clv24).sum() >= 2 else (np.nan, np.nan)
    halves = (np.nan, np.nan)
    if n >= 4:
        med = np.median(cl["ts"])
        h1, h2 = cl[cl["ts"] <= med], cl[cl["ts"] > med]
        halves = (wmean(h1["edge_c"].to_numpy(), h1["w_c"].to_numpy()),
                  wmean(h2["edge_c"].to_numpy(), h2["w_c"].to_numpy()))
    e90 = cl[cl["ts"] >= now - 90 * DAY]
    e365 = cl[cl["ts"] >= now - 365 * DAY]
    decay = np.nan
    if len(e90) >= 20 and len(e365) >= 20:  # на коротком окне оценка затухания слишком шумная
        a = wmean(e90["edge_c"].to_numpy(), e90["w_c"].to_numpy())
        b = wmean(e365["edge_c"].to_numpy(), e365["w_c"].to_numpy())
        decay = a / b if b > 0 else np.nan
    recent = closed[closed["closed_ts"] >= now - 90 * DAY]
    ret90 = float(recent["pnl_usd"].sum() / recent["cost_usd"].sum()) if len(recent) and recent["cost_usd"].sum() > 0 else np.nan
    hp = cls.hold_profile(closed)
    return {
        "entity_id": e, "n_clusters": n,
        "total_cost": float(pos_e["cost_usd"].sum()),
        "history_days": float((now - pos_e["opened_ts"].min()) / DAY) if len(pos_e) else 0.0,
        "edge_raw": e_raw, "edge_shrunk": e_shr, "edge_ci_lo": lo, "edge_ci_hi": hi,
        "clv_1h": wmean(cl["clv_1h"].to_numpy(float), w) if n else np.nan,
        "clv_24h": wmean(clv24, w) if n else np.nan, "clv_ci_lo": clv_lo,
        "edge_half1": halves[0], "edge_half2": halves[1], "decay_ratio": decay,
        "return_90d": ret90, **hp,
    }


def _category_scores(e: str, cl: pd.DataFrame, params: Params, rng, heavy: bool = True) -> list[dict]:
    out = []
    for cat, g in cl.groupby(cl["category"].fillna("other")):
        x, w = g["edge_c"].to_numpy(float), g["w_c"].to_numpy(float)
        n = len(g)
        es = shrink(wmean(x, w), n, params.shrink_k)
        need = heavy and n >= params.min_clusters_category      # тема с малым числом событий не допускается
        lo, _ = bootstrap_wmean(x, w, max(500, params.bootstrap_b // 5), rng, k=params.shrink_k) \
            if n >= 2 and need else (np.nan, np.nan)
        ok = n >= params.min_clusters_category and es > 0 and np.isfinite(lo) and lo > 0
        out.append({"entity_id": e, "category": str(cat), "n_clusters": n, "edge_shrunk": es,
                    "edge_ci_lo": lo, "status": "approved" if ok else "not_approved"})
    return out


def score_entities(pos: pd.DataFrame, trades: pd.DataFrame, wallets: pd.DataFrame, markets: pd.DataFrame,
                   prices: pd.DataFrame, params: Params, now: int, seed: int = 7,
                   entities: list[str] | None = None, pvalue_pool: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """pvalue_pool — p-value других игроков, проверенных раньше ({entity: p}). Поправка на множественные
    проверки (BH) считается по всем проверенным игрокам, а не только по пересчитываемым сейчас: иначе
    у одиночной проверки q = p и случайный «везунчик» проходит отбор легче, чем при общем пересчёте."""
    rng = np.random.default_rng(seed)
    if pos.empty:
        return pd.DataFrame(), pd.DataFrame()
    pos = pos[pos["opened_ts"] <= now].copy()
    if "resolved_ts" not in pos:
        pos = pos.merge(markets[["market_id", "resolved_ts"]], on="market_id", how="left")
    pos = add_clv(pos, prices, params)
    closed = closed_positions(pos, now)
    clusters = cluster_table(closed)
    trades = trades[trades["ts"] <= now]
    ent_of = pos.drop_duplicates("wallet").set_index("wallet")["entity_id"].to_dict()
    trades = trades.assign(entity_id=trades["wallet"].map(lambda x: ent_of.get(x, x)))
    first_seen = {}
    if not wallets.empty:
        ws = wallets.assign(entity_id=wallets["wallet"].map(lambda x: ent_of.get(x, x)))
        first_seen = ws.groupby("entity_id")["first_seen_ts"].min().to_dict()
    vol = markets.set_index("market_id")["volume_usd"].to_dict() if not markets.empty else {}
    last_trade = trades.groupby("entity_id")["ts"].max().to_dict()

    rows, cat_rows = [], []
    ids = entities or sorted(pos["entity_id"].unique())
    cl_by = dict(tuple(clusters.groupby("entity_id"))) if not clusters.empty else {}
    closed_by = dict(tuple(closed.groupby("entity_id"))) if not closed.empty else {}
    pos_by = dict(tuple(pos.groupby("entity_id")))
    tr_by = dict(tuple(trades.groupby("entity_id"))) if not trades.empty else {}
    for e in ids:
        cl = cl_by.get(e, clusters.iloc[0:0])
        cl_e = closed_by.get(e, closed.iloc[0:0])
        pos_e = pos_by.get(e, pos.iloc[0:0])
        tr_e = tr_by.get(e, trades.iloc[0:0])
        # быстрый путь: мало событий — бутстреп и проверка на удачу не нужны (статус «мало данных»)
        heavy = len(cl) >= min(params.min_clusters, params.min_clusters_category)
        core = _entity_core(e, cl, cl_e, pos_e, now, params, rng, heavy=heavy)
        klass, flags = cls.classify(tr_e, pos_e, cl_e, core["n_clusters"], first_seen.get(e), vol, params)
        core["last_trade_ts"] = int(last_trade.get(e, 0))
        core["class"] = klass
        core["flags"] = flags
        core["buckets"] = bucket_edges(cl_e, params.price_buckets)
        core["categories"] = sorted({c for c in cl["category"].dropna()}) if len(cl) else []
        core["active"] = now - core["last_trade_ts"] <= params.max_inactive_days * DAY
        core["base_ok"] = (core["n_clusters"] >= params.min_clusters
                           and core["history_days"] >= params.min_history_days
                           and core["active"]
                           and core["total_cost"] >= params.min_total_cost_usd)
        core["pvalue"] = np.nan
        if core["base_ok"] and len(cl):
            core["pvalue"] = null_pvalue(cl["p_rep"].to_numpy(), cl["r_rep"].to_numpy(),
                                         cl["w_c"].to_numpy(), params.null_sims, rng)
        rows.append(core)
        if len(cl):
            cat_rows.extend(_category_scores(e, cl, params, rng, heavy=heavy))

    df = pd.DataFrame(rows)
    df["qvalue"] = np.nan
    tested = df["pvalue"].notna()
    if tested.any():
        pool = {k: float(v) for k, v in (pvalue_pool or {}).items()
                if v is not None and np.isfinite(v) and k not in set(df["entity_id"])}
        mine = df.loc[tested, "pvalue"].to_numpy()
        q = bh_qvalues(np.concatenate([mine, np.fromiter(pool.values(), float, len(pool))]))
        df.loc[tested, "qvalue"] = q[:len(mine)]
    df["status"] = df.apply(lambda r: _status(r, params), axis=1)
    df["score"] = np.nan
    ok = df["status"] == "approved"
    if ok.any():
        a, b, c, d = params.rating_weights
        sub = df[ok]
        df.loc[ok, "score"] = (a * zscore(sub["edge_ci_lo"]) + b * zscore(sub["clv_24h"].fillna(0))
                               + c * zscore(np.minimum(sub["edge_half1"], sub["edge_half2"]))
                               + d * zscore(sub["hold_share"].fillna(0)))
    return df, pd.DataFrame(cat_rows)


def approval_checks(r, params: Params) -> dict:
    """Условия допуска 6.7 — по отдельности (для объяснения «почему не подходит»)."""
    def fin(v):
        return v is not None and np.isfinite(v)
    return {
        "enough_history": r["n_clusters"] >= params.min_clusters and r["history_days"] >= params.min_history_days,
        "active": bool(r["active"]),
        "enough_volume": r["total_cost"] >= params.min_total_cost_usd,
        "edge_positive": fin(r["edge_ci_lo"]) and r["edge_ci_lo"] > 0,
        "stable": fin(r["edge_half1"]) and fin(r["edge_half2"]) and r["edge_half1"] > 0 and r["edge_half2"] > 0,
        "not_luck": fin(r.get("qvalue", np.nan)) and r["qvalue"] <= params.fdr_q,
        "clv_positive": fin(r["clv_ci_lo"]) and r["clv_ci_lo"] > 0,
        "holds_positions": fin(r["hold_share"]) and r["hold_share"] >= params.min_hold_share,
        "class_ok": r["class"] == "normal",
    }


def _status(r, params: Params) -> str:
    if r["class"] != "normal":
        return "excluded"
    if not r["base_ok"]:
        return "insufficient"
    if all(approval_checks(r, params).values()):
        if np.isfinite(r["decay_ratio"]) and r["decay_ratio"] < params.decay_ratio_min:
            return "watch"
        return "approved"
    return "candidate"


# ---------------------------------------------------------------- работа с БД

TRADE_COLS = "trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd, source"


def drop_conversions(db: DB, pos: pd.DataFrame, wallets: list[str] | None = None) -> pd.DataFrame:
    """Позиции в событиях, где кошелёк делал конвертацию neg-risk, в оценку не идут."""
    from ..ingest.pipeline import conversion_events
    bad = conversion_events(db, wallets)
    if not bad or pos.empty:
        return pos
    key = pd.Series(list(zip(pos["wallet"], pos["event_id"].fillna(pos["market_id"]))), index=pos.index)
    return pos[~key.isin(bad)]


def load_frames(db: DB, entities: list[str] | None = None) -> dict:
    """Данные для скоринга. entities — только эти игроки (проверка одного кита):
    читаются их сделки, позиции, рынки и цены, а не вся база."""
    f = _load_frames(db, entities)
    f["pos"] = drop_conversions(db, f["pos"], sorted(set(f["pos"]["wallet"])) if entities and len(f["pos"]) else None)
    return f


def _load_frames(db: DB, entities: list[str] | None = None) -> dict:
    if not entities:
        return {
            "pos": db.frame("SELECT * FROM positions"),
            "trades": db.frame(f"SELECT {TRADE_COLS} FROM trades"),
            "wallets": db.frame("SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets"),
            "markets": db.frame("SELECT market_id, event_id, category, volume_usd, resolved_ts, status FROM markets"),
            # цены нужны только по токенам позиций (CLV)
            "prices": db.frame("SELECT token_id, ts, price FROM price_history WHERE token_id IN "
                               "(SELECT DISTINCT token_id FROM positions)"),
        }
    pe = ",".join("?" for _ in entities)
    pos = db.frame(f"SELECT * FROM positions WHERE entity_id IN ({pe})", entities)
    wallets = sorted(set(pos["wallet"]) | set(entities)) if len(pos) else list(entities)
    pw = ",".join("?" for _ in wallets)
    pos_sub = f"SELECT DISTINCT {{col}} FROM positions WHERE entity_id IN ({pe})"
    return {
        "pos": pos,
        "trades": db.frame(f"SELECT {TRADE_COLS} FROM trades WHERE wallet IN ({pw})", wallets),
        "wallets": db.frame(f"SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets WHERE wallet IN ({pw})",
                            wallets),
        "markets": db.frame("SELECT market_id, event_id, category, volume_usd, resolved_ts, status FROM markets "
                            f"WHERE market_id IN ({pos_sub.format(col='market_id')})", entities),
        "prices": db.frame(f"SELECT token_id, ts, price FROM price_history WHERE token_id IN "
                           f"({pos_sub.format(col='token_id')})", entities),
    }


def run_scoring(db: DB, params: Params, now: int | None = None, seed: int = 7,
                entities: list[str] | None = None, add_new: bool = True) -> pd.DataFrame:
    """entities — пересчитать только этих игроков (киты из списка); add_new — добавлять ли
    в живой список новых допущенных игроков (только при автопоиске)."""
    now = now or now_ts()
    f = load_frames(db, entities)
    if f["pos"].empty:
        return pd.DataFrame()
    if f["prices"].empty:
        f["prices"] = pd.DataFrame(columns=["token_id", "ts", "price"])
    df, cats = score_entities(f["pos"], f["trades"], f["wallets"], f["markets"], f["prices"], params, now, seed,
                              entities=entities, pvalue_pool=pvalue_pool(db))
    save_scores(db, df, cats, now)
    refresh_watchlist(db, df, params, add_new=add_new)
    return df


def pvalue_pool(db: DB) -> dict:
    """Последние p-value всех когда-либо проверенных игроков — общая база для поправки BH."""
    rows = db.query("SELECT s.entity_id, s.pvalue FROM wallet_scores s JOIN (SELECT entity_id, MAX(computed_at) AS m "
                    "FROM wallet_scores GROUP BY entity_id) t ON s.entity_id=t.entity_id AND s.computed_at=t.m "
                    "WHERE s.pvalue IS NOT NULL")
    return {r["entity_id"]: r["pvalue"] for r in rows}


def save_scores(db: DB, df: pd.DataFrame, cats: pd.DataFrame, now: int) -> None:
    rows, flag_rows = [], []
    for r in df.to_dict("records"):
        metrics = {"buckets": r["buckets"], "categories": r["categories"], "flags": r["flags"]}
        rows.append({k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in {
            "entity_id": r["entity_id"], "computed_at": now, "window_days": 0,
            "n_clusters": int(r["n_clusters"]), "total_cost": r["total_cost"], "history_days": r["history_days"],
            "last_trade_ts": int(r["last_trade_ts"]),
            "edge_raw": r["edge_raw"], "edge_shrunk": r["edge_shrunk"], "edge_ci_lo": r["edge_ci_lo"],
            "edge_ci_hi": r["edge_ci_hi"], "clv_1h": r["clv_1h"], "clv_24h": r["clv_24h"], "clv_ci_lo": r["clv_ci_lo"],
            "pvalue": r["pvalue"], "qvalue": r["qvalue"], "edge_half1": r["edge_half1"], "edge_half2": r["edge_half2"],
            "decay_ratio": r["decay_ratio"], "hold_share": r["hold_share"], "median_hold_h": r["median_hold_h"],
            "return_90d": r["return_90d"], "class": r["class"], "status": r["status"], "score": r["score"],
            "metrics": json.dumps(metrics, default=float),
        }.items()})
        for k, v in r["flags"].items():
            if v is not None and np.isfinite(v):
                flag_rows.append({"entity_id": r["entity_id"], "flag": k, "value": float(v), "computed_at": now})
    cat_rows = []
    if not cats.empty:
        for r in cats.to_dict("records"):
            cat_rows.append({**r, "computed_at": now, "edge_ci_lo": None if not np.isfinite(r["edge_ci_lo"]) else r["edge_ci_lo"],
                             "n_clusters": int(r["n_clusters"])})
    with db.tx():
        db.upsert_many("wallet_scores", rows, ["entity_id", "computed_at"])
        db.upsert_many("wallet_category_scores", cat_rows, ["entity_id", "category", "computed_at"])
        db.upsert_many("wallet_flags", flag_rows, ["entity_id", "flag", "computed_at"])


def refresh_watchlist(db: DB, df: pd.DataFrame, params: Params, add_new: bool = True) -> None:
    """Живой список: топ-N допущенных по рейтингу. Решения пользователя (watch/off/copy) не перетираются.
    add_new=False — новых китов система сама не добавляет (данные собираются только по списку пользователя)."""
    top = df[df["status"] == "approved"].sort_values("score", ascending=False).head(params.live_list_size)
    for e in (top["entity_id"] if add_new else []):
        if not db.one("SELECT 1 AS x FROM watchlist WHERE entity_id=?", (e,)):
            db.upsert("watchlist", {"entity_id": e, "mode": "copy", "added_by": "system",
                                    "added_ts": now_ts(), "categories_off": "[]"}, ["entity_id"])
    # кит, переставший проходить отбор, перестаёт копироваться автоматически
    for r in df[df["status"].isin(["excluded", "watch", "candidate", "insufficient"])].itertuples():
        row = db.one("SELECT mode, added_by FROM watchlist WHERE entity_id=?", (r.entity_id,))
        if row and row["added_by"] == "system" and row["mode"] == "copy":
            db.execute("UPDATE watchlist SET mode='watch' WHERE entity_id=?", (r.entity_id,))
            db.notify("whale_status", f"Кит {short(r.entity_id)} больше не копируется",
                      "Его преимущество перестало подтверждаться — оставили в режиме «Только следить».", r.entity_id)


def latest_scores(db: DB) -> pd.DataFrame:
    return pd.DataFrame(db.query(
        "SELECT s.* FROM wallet_scores s JOIN (SELECT entity_id, MAX(computed_at) AS m FROM wallet_scores "
        "GROUP BY entity_id) t ON s.entity_id=t.entity_id AND s.computed_at=t.m"))


def latest_category_scores(db: DB, entity_id: str) -> dict[str, dict]:
    rows = db.query("SELECT * FROM wallet_category_scores WHERE entity_id=? AND computed_at="
                    "(SELECT MAX(computed_at) FROM wallet_category_scores WHERE entity_id=?)", (entity_id, entity_id))
    return {r["category"]: r for r in rows}


def short(entity_id: str) -> str:
    """Короткое имя кита для интерфейса (A12-подобное)."""
    s = entity_id.replace("E-", "")
    s = s[2:] if s.startswith("0x") else s
    return (s[:2] + s[-2:]).upper() if len(s) > 4 else s.upper()   # начало и конец адреса — различимы
