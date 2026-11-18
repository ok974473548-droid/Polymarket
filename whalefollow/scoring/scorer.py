"""Скоринг игроков (ТЗ 6.3–6.9) и формирование живого списка китов.

Вход — позиции (с entity_id), сделки, кошельки, рынки, история цен.
Все расчёты делаются «на момент now»: в бэктесте now — конец окна отбора,
и данные после него не используются.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import polars as pl

from .. import frames
from ..config import Params
from ..db import DB, now_ts
from . import classify as cls
from .classify import Cols, cols
from .metrics import add_clv, bucket_edges, closed_positions, cluster_table
from .stats import bh_qvalues, bootstrap_wmean, null_pvalue, shrink, wmean, zscore

log = logging.getLogger(__name__)
DAY = 86400
NAN = float("nan")
CL_COLS = ["edge_c", "w_c", "clv_1h", "clv_24h", "ts", "category", "p_rep", "r_rep"]
CLOSED_COLS = [
    "closed_ts",
    "opened_ts",
    "pnl_usd",
    "cost_usd",
    "avg_entry",
    "exit_value_per_share",
    "edge",
    "held_to_resolution",
]
POS_COLS = [
    "cost_usd",
    "opened_ts",
    "closed_ts",
    "status",
    "remaining_shares",
    "avg_entry",
    "market_id",
    "event_id",
    "clv_24h",
]
NESTED = ("flags", "buckets", "categories")


def _n(c: Cols) -> int:
    return len(next(iter(c.values()))) if c else 0


def _sub(c: Cols, m: np.ndarray) -> Cols:
    return {k: v[m] for k, v in c.items()}


def _entity_core(
    e: str,
    cl: Cols,
    closed: Cols,
    pos_e: Cols,
    now: int,
    params: Params,
    rng: np.random.Generator,
    heavy: bool = True,
) -> dict:
    """heavy=False — без бутстрепа: у игрока слишком мало событий, отбор он всё равно не пройдёт."""
    n = _n(cl)
    x, w = (cl["edge_c"], cl["w_c"]) if n else (np.zeros(0), np.zeros(0))
    e_raw = wmean(x, w) if n else NAN
    e_shr = shrink(e_raw, n, params.shrink_k)
    lo, hi = bootstrap_wmean(x, w, params.bootstrap_b, rng, k=params.shrink_k) if n and heavy else (NAN, NAN)
    clv24 = cl["clv_24h"] if n else np.zeros(0)
    clv_lo, _ = (
        bootstrap_wmean(clv24, w, max(500, params.bootstrap_b // 5), rng)
        if heavy and np.isfinite(clv24).sum() >= 2
        else (NAN, NAN)
    )
    halves = (NAN, NAN)
    if n >= 4:
        ts = cl["ts"]
        med = np.median(ts)
        h1, h2 = ts <= med, ts > med
        halves = (wmean(x[h1], w[h1]), wmean(x[h2], w[h2]))
    decay = NAN
    if n:
        m90, m365 = cl["ts"] >= now - 90 * DAY, cl["ts"] >= now - 365 * DAY
        if m90.sum() >= 20 and m365.sum() >= 20:  # на коротком окне оценка затухания слишком шумная
            a = wmean(x[m90], w[m90])
            b = wmean(x[m365], w[m365])
            decay = a / b if b > 0 else NAN
    ret90 = NAN
    if _n(closed):
        recent = closed["closed_ts"] >= now - 90 * DAY
        cost90 = np.nansum(closed["cost_usd"][recent])
        if recent.any() and cost90 > 0:
            ret90 = float(np.nansum(closed["pnl_usd"][recent]) / cost90)
    hp = cls.hold_profile(closed)
    n_pos = _n(pos_e)
    return {
        "entity_id": e,
        "n_clusters": n,
        "total_cost": float(np.nansum(pos_e["cost_usd"])) if n_pos else 0.0,
        "history_days": float((now - np.nanmin(pos_e["opened_ts"])) / DAY) if n_pos else 0.0,
        "edge_raw": e_raw,
        "edge_shrunk": e_shr,
        "edge_ci_lo": lo,
        "edge_ci_hi": hi,
        "clv_1h": wmean(cl["clv_1h"], w) if n else NAN,
        "clv_24h": wmean(clv24, w) if n else NAN,
        "clv_ci_lo": clv_lo,
        "edge_half1": halves[0],
        "edge_half2": halves[1],
        "decay_ratio": decay,
        "return_90d": ret90,
        **hp,
    }


def _category_scores(e: str, cl: Cols, params: Params, rng, heavy: bool = True) -> list[dict]:
    out = []
    cats = np.array(["other" if c is None else str(c) for c in cl["category"]], dtype=object)
    for cat in sorted(set(cats)):
        m = cats == cat
        x, w = cl["edge_c"][m], cl["w_c"][m]
        n = int(m.sum())
        es = shrink(wmean(x, w), n, params.shrink_k)
        need = heavy and n >= params.min_clusters_category  # тема с малым числом событий не допускается
        lo, _ = (
            bootstrap_wmean(x, w, max(500, params.bootstrap_b // 5), rng, k=params.shrink_k)
            if n >= 2 and need
            else (NAN, NAN)
        )
        ok = n >= params.min_clusters_category and es > 0 and np.isfinite(lo) and lo > 0
        out.append(
            {
                "entity_id": e,
                "category": str(cat),
                "n_clusters": n,
                "edge_shrunk": es,
                "edge_ci_lo": lo,
                "status": "approved" if ok else "not_approved",
            }
        )
    return out


def _partition(df: pl.DataFrame, names: list[str]) -> dict[str, Cols]:
    """Таблица → {игрок: массивы numpy}. Колонки переводятся в numpy один раз, затем режутся по игрокам
    (порядок строк внутри игрока сохраняется)."""
    if df.is_empty():
        return {}
    names = [c for c in names if c in df.columns]
    ent = df["entity_id"].to_numpy().astype(str)
    keys, inv, counts = np.unique(ent, return_inverse=True, return_counts=True)
    order = np.argsort(inv, kind="stable")
    bounds = np.cumsum(counts)[:-1]
    full = cols(df, names)
    parts = {c: np.split(v[order], bounds) for c, v in full.items()}
    return {str(k): {c: parts[c][i] for c in names} for i, k in enumerate(keys)}


def _empty_cols(names: list[str]) -> Cols:
    return {c: np.zeros(0) for c in names}


def score_entities(
    pos: pl.DataFrame,
    trades: pl.DataFrame,
    wallets: pl.DataFrame,
    markets: pl.DataFrame,
    prices: pl.DataFrame,
    params: Params,
    now: int,
    seed: int = 7,
    entities: list[str] | None = None,
    pvalue_pool: dict | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """pvalue_pool — p-value других игроков, проверенных раньше ({entity: p}). Поправка на множественные
    проверки (BH) считается по всем проверенным игрокам, а не только по пересчитываемым сейчас: иначе
    у одиночной проверки q = p и случайный «везунчик» проходит отбор легче, чем при общем пересчёте.

    Возвращает (оценки игроков, оценки по темам) — таблицы polars; вложенные flags/buckets/categories
    в оценках игроков хранятся как объекты Python."""
    rng = np.random.default_rng(seed)
    if pos.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    pos = pos.filter(pl.col("opened_ts") <= now)
    if "resolved_ts" not in pos.columns:
        pos = pos.join(
            frames.cast(markets.select("market_id", "resolved_ts")).unique("market_id", keep="first"),
            on="market_id",
            how="left",
            maintain_order="left",
        )
    pos = add_clv(pos, prices, params)
    closed = closed_positions(pos, now)
    clusters = cluster_table(closed)
    trades = trades.filter(pl.col("ts") <= now)
    ent_of = pos.select("wallet", "entity_id").unique("wallet", keep="first", maintain_order=True)
    if "entity_id" in trades.columns:
        trades = trades.drop("entity_id")
    trades = trades.join(ent_of, on="wallet", how="left", maintain_order="left").with_columns(
        entity_id=pl.coalesce("entity_id", "wallet")
    )
    first_seen: dict = {}
    if not wallets.is_empty():
        ws = wallets.join(ent_of, on="wallet", how="left").with_columns(entity_id=pl.coalesce("entity_id", "wallet"))
        first_seen = dict(ws.group_by("entity_id").agg(pl.col("first_seen_ts").min()).iter_rows())
    vol = dict(markets.select("market_id", "volume_usd").iter_rows()) if not markets.is_empty() else {}
    last_trade = dict(trades.group_by("entity_id").agg(pl.col("ts").max()).iter_rows()) if trades.height else {}
    maker = cls.maker_trade_flags(trades)

    rows, cat_rows = [], []
    ids = entities or sorted(pos["entity_id"].unique().to_list())
    cl_by = _partition(clusters, CL_COLS)
    closed_by = _partition(closed, CLOSED_COLS)
    pos_by = _partition(pos, POS_COLS)
    for e in ids:
        cl = cl_by.get(e) or _empty_cols(CL_COLS)
        cl_e = closed_by.get(e) or _empty_cols(CLOSED_COLS)
        pos_e = pos_by.get(e) or _empty_cols(POS_COLS)
        n_cl = _n(cl)
        # быстрый путь: мало событий — бутстреп и проверка на удачу не нужны (статус «мало данных»)
        heavy = n_cl >= min(params.min_clusters, params.min_clusters_category)
        core = _entity_core(e, cl, cl_e, pos_e, now, params, rng, heavy=heavy)
        fs = first_seen.get(e)
        klass, flags = cls.classify(
            maker.get(e), pos_e, cl_e, core["n_clusters"], float(fs) if fs is not None else None, vol, params
        )
        core["last_trade_ts"] = int(last_trade.get(e) or 0)
        core["class"] = klass
        core["flags"] = flags
        core["buckets"] = bucket_edges(cl_e, params.price_buckets)
        core["categories"] = sorted({c for c in cl["category"] if c is not None}) if n_cl else []
        core["active"] = now - core["last_trade_ts"] <= params.max_inactive_days * DAY
        core["base_ok"] = (
            core["n_clusters"] >= params.min_clusters
            and core["history_days"] >= params.min_history_days
            and core["active"]
            and core["total_cost"] >= params.min_total_cost_usd
        )
        core["pvalue"] = NAN
        if core["base_ok"] and n_cl:
            core["pvalue"] = null_pvalue(cl["p_rep"], cl["r_rep"], cl["w_c"], params.null_sims, rng)
        rows.append(core)
        if n_cl:
            cat_rows.extend(_category_scores(e, cl, params, rng, heavy=heavy))

    pv = np.array([r["pvalue"] for r in rows], float)
    tested = np.isfinite(pv)
    q_all = np.full(len(rows), NAN)
    if tested.any():
        mine_ids = {r["entity_id"] for r in rows}
        pool = {
            k: float(v)
            for k, v in (pvalue_pool or {}).items()
            if v is not None and np.isfinite(v) and k not in mine_ids
        }
        q = bh_qvalues(np.concatenate([pv[tested], np.fromiter(pool.values(), float, len(pool))]))
        q_all[tested] = q[: int(tested.sum())]
    for r, qv in zip(rows, q_all):
        r["qvalue"] = float(qv)
        r["status"] = _status(r, params)
        r["score"] = NAN
    ok = [i for i, r in enumerate(rows) if r["status"] == "approved"]
    if ok:
        a, b, c, d = params.rating_weights
        sub = [rows[i] for i in ok]
        col = lambda k, fill=None: np.array(
            [(fill if fill is not None and not np.isfinite(r[k]) else r[k]) for r in sub], float
        )
        sc = (
            a * zscore(col("edge_ci_lo"))
            + b * zscore(col("clv_24h", 0.0))
            + c * zscore(np.minimum(col("edge_half1"), col("edge_half2")))
            + d * zscore(col("hold_share", 0.0))
        )
        for i, v in zip(ok, sc):
            rows[i]["score"] = float(v)
    return to_frame(rows), pl.DataFrame(cat_rows) if cat_rows else pl.DataFrame()


def to_frame(rows: list[dict]) -> pl.DataFrame:
    """Строки оценок → таблица polars (вложенные значения — объекты Python)."""
    if not rows:
        return pl.DataFrame()
    flat = [{k: v for k, v in r.items() if k not in NESTED} for r in rows]
    df = pl.DataFrame(flat, infer_schema_length=None, nan_to_null=False)
    return df.with_columns([pl.Series(k, [r[k] for r in rows], dtype=pl.Object) for k in NESTED])


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


def drop_conversions(db: DB, pos: pl.DataFrame, wallets: list[str] | None = None) -> pl.DataFrame:
    """Позиции в событиях, где кошелёк делал конвертацию neg-risk, в оценку не идут."""
    from ..ingest.pipeline import conversion_events

    bad = conversion_events(db, wallets)
    if not bad or pos.is_empty():
        return pos
    bad_df = pl.DataFrame(
        {"wallet": [w for w, _ in bad], "_ev": [e for _, e in bad]}, schema={"wallet": pl.Utf8, "_ev": pl.Utf8}
    )
    return (
        pos.with_columns(_ev=pl.coalesce("event_id", "market_id"))
        .join(bad_df, on=["wallet", "_ev"], how="anti", maintain_order="left")
        .drop("_ev")
    )


def load_frames(db: DB, entities: list[str] | None = None) -> dict:
    """Данные для скоринга. entities — только эти игроки (проверка одного кита):
    читаются их сделки, позиции, рынки и цены, а не вся база."""
    f = _load_frames(db, entities)
    f["pos"] = drop_conversions(
        db, f["pos"], sorted(set(f["pos"]["wallet"].to_list())) if entities and f["pos"].height else None
    )
    return f


POS_SELECT = "SELECT * FROM positions"


def _load_frames(db: DB, entities: list[str] | None = None) -> dict:
    if not entities:
        return {
            "pos": frames.read(db, POS_SELECT),
            "trades": frames.read_history(db, f"SELECT {TRADE_COLS} FROM trades"),
            "wallets": frames.read(db, "SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets"),
            "markets": frames.read(
                db, "SELECT market_id, event_id, category, volume_usd, resolved_ts, status FROM markets"
            ),
            # цены нужны только по токенам позиций (CLV); история большая — читает DuckDB
            "prices": frames.read_history(
                db,
                "SELECT token_id, ts, price FROM price_history WHERE token_id IN "
                "(SELECT DISTINCT token_id FROM positions)",
            ),
        }
    pe = ",".join("?" for _ in entities)
    pos = frames.read(db, f"{POS_SELECT} WHERE entity_id IN ({pe})", entities)
    wallets = sorted(set(pos["wallet"].to_list()) | set(entities)) if pos.height else list(entities)
    pw = ",".join("?" for _ in wallets)
    pos_sub = f"SELECT DISTINCT {{col}} FROM positions WHERE entity_id IN ({pe})"
    return {
        "pos": pos,
        "trades": frames.read(db, f"SELECT {TRADE_COLS} FROM trades WHERE wallet IN ({pw})", wallets),
        "wallets": frames.read(
            db, f"SELECT wallet, first_seen_ts, owner_eoa, funder_wallet FROM wallets WHERE wallet IN ({pw})", wallets
        ),
        "markets": frames.read(
            db,
            "SELECT market_id, event_id, category, volume_usd, resolved_ts, status FROM markets "
            f"WHERE market_id IN ({pos_sub.format(col='market_id')})",
            entities,
        ),
        "prices": frames.read(
            db,
            f"SELECT token_id, ts, price FROM price_history WHERE token_id IN ({pos_sub.format(col='token_id')})",
            entities,
        ),
    }


def run_scoring(
    db: DB,
    params: Params,
    now: int | None = None,
    seed: int = 7,
    entities: list[str] | None = None,
    add_new: bool = True,
) -> pl.DataFrame:
    """entities — пересчитать только этих игроков (киты из списка); add_new — добавлять ли
    в живой список новых допущенных игроков (только при автопоиске)."""
    now = now or now_ts()
    f = load_frames(db, entities)
    if f["pos"].is_empty():
        return pl.DataFrame()
    df, cats = score_entities(
        f["pos"],
        f["trades"],
        f["wallets"],
        f["markets"],
        f["prices"],
        params,
        now,
        seed,
        entities=entities,
        pvalue_pool=pvalue_pool(db),
    )
    save_scores(db, df, cats, now)
    refresh_watchlist(db, df, params, add_new=add_new)
    return df


def pvalue_pool(db: DB) -> dict:
    """Последние p-value всех когда-либо проверенных игроков — общая база для поправки BH."""
    rows = db.query(
        "SELECT s.entity_id, s.pvalue FROM wallet_scores s JOIN (SELECT entity_id, MAX(computed_at) AS m "
        "FROM wallet_scores GROUP BY entity_id) t ON s.entity_id=t.entity_id AND s.computed_at=t.m "
        "WHERE s.pvalue IS NOT NULL"
    )
    return {r["entity_id"]: r["pvalue"] for r in rows}


def _fin(v: Any) -> Any:
    return None if isinstance(v, float) and not np.isfinite(v) else v


def save_scores(db: DB, df: pl.DataFrame, cats: pl.DataFrame, now: int) -> None:
    rows, flag_rows = [], []
    for r in df.to_dicts():
        metrics = {"buckets": r["buckets"], "categories": r["categories"], "flags": r["flags"]}
        rows.append(
            {
                k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                for k, v in {
                    "entity_id": r["entity_id"],
                    "computed_at": now,
                    "window_days": 0,
                    "n_clusters": int(r["n_clusters"]),
                    "total_cost": r["total_cost"],
                    "history_days": r["history_days"],
                    "last_trade_ts": int(r["last_trade_ts"]),
                    "edge_raw": r["edge_raw"],
                    "edge_shrunk": r["edge_shrunk"],
                    "edge_ci_lo": r["edge_ci_lo"],
                    "edge_ci_hi": r["edge_ci_hi"],
                    "clv_1h": r["clv_1h"],
                    "clv_24h": r["clv_24h"],
                    "clv_ci_lo": r["clv_ci_lo"],
                    "pvalue": r["pvalue"],
                    "qvalue": r["qvalue"],
                    "edge_half1": r["edge_half1"],
                    "edge_half2": r["edge_half2"],
                    "decay_ratio": r["decay_ratio"],
                    "hold_share": r["hold_share"],
                    "median_hold_h": r["median_hold_h"],
                    "return_90d": r["return_90d"],
                    "class": r["class"],
                    "status": r["status"],
                    "score": r["score"],
                    "metrics": json.dumps(metrics, default=float),
                }.items()
            }
        )
        for k, v in r["flags"].items():
            if v is not None and np.isfinite(v):
                flag_rows.append({"entity_id": r["entity_id"], "flag": k, "value": float(v), "computed_at": now})
    cat_rows = []
    if not cats.is_empty():
        for r in cats.to_dicts():
            cat_rows.append(
                {
                    **r,
                    "computed_at": now,
                    "edge_shrunk": _fin(r["edge_shrunk"]),
                    "edge_ci_lo": _fin(r["edge_ci_lo"]),
                    "n_clusters": int(r["n_clusters"]),
                }
            )
    with db.tx():
        db.upsert_many("wallet_scores", rows, ["entity_id", "computed_at"])
        db.upsert_many("wallet_category_scores", cat_rows, ["entity_id", "category", "computed_at"])
        db.upsert_many("wallet_flags", flag_rows, ["entity_id", "flag", "computed_at"])


def refresh_watchlist(db: DB, df: pl.DataFrame, params: Params, add_new: bool = True) -> None:
    """Живой список: топ-N допущенных по рейтингу. Решения пользователя (watch/off/copy) не перетираются.
    add_new=False — новых китов система сама не добавляет (данные собираются только по списку пользователя)."""
    if df.is_empty():
        return
    rows = df.select("entity_id", "status", "score").to_dicts()
    approved = [r for r in rows if r["status"] == "approved"]
    approved.sort(key=lambda r: -(r["score"] if r["score"] is not None and np.isfinite(r["score"]) else -np.inf))
    for r in approved[: params.live_list_size] if add_new else []:
        e = r["entity_id"]
        if not db.one("SELECT 1 AS x FROM watchlist WHERE entity_id=?", (e,)):
            db.upsert(
                "watchlist",
                {"entity_id": e, "mode": "copy", "added_by": "system", "added_ts": now_ts(), "categories_off": "[]"},
                ["entity_id"],
            )
    # кит, переставший проходить отбор, перестаёт копироваться автоматически
    for r in rows:
        if r["status"] not in ("excluded", "watch", "candidate", "insufficient"):
            continue
        row = db.one("SELECT mode, added_by FROM watchlist WHERE entity_id=?", (r["entity_id"],))
        if row and row["added_by"] == "system" and row["mode"] == "copy":
            db.execute("UPDATE watchlist SET mode='watch' WHERE entity_id=?", (r["entity_id"],))
            db.notify(
                "whale_status",
                f"Кит {short(r['entity_id'])} больше не копируется",
                "Его преимущество перестало подтверждаться — оставили в режиме «Только следить».",
                r["entity_id"],
            )


def latest_scores(db: DB) -> pl.DataFrame:
    return frames.read(
        db,
        "SELECT s.* FROM wallet_scores s JOIN (SELECT entity_id, MAX(computed_at) AS m FROM wallet_scores "
        "GROUP BY entity_id) t ON s.entity_id=t.entity_id AND s.computed_at=t.m",
    )


def latest_category_scores(db: DB, entity_id: str) -> dict[str, dict]:
    rows = db.query(
        "SELECT * FROM wallet_category_scores WHERE entity_id=? AND computed_at="
        "(SELECT MAX(computed_at) FROM wallet_category_scores WHERE entity_id=?)",
        (entity_id, entity_id),
    )
    return {r["category"]: r for r in rows}


def short(entity_id: str) -> str:
    """Короткое имя кита для интерфейса (A12-подобное)."""
    s = entity_id.replace("E-", "")
    s = s[2:] if s.startswith("0x") else s
    return (s[:2] + s[-2:]).upper() if len(s) > 4 else s.upper()  # начало и конец адреса — различимы
