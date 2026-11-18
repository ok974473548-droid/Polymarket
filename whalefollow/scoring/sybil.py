"""Антисибил: объединение кошельков одного владельца (ТЗ 7.2).

Сигналы связи:
  L1 общий источник финансирования (кроме allowlist бирж/мостов)  0.8
  L2 прямые переводы между a и b                                    0.9
  L3 Jaccard рынков ≥ 0.6 при |M| ≥ 20                               0.5
  L4 синхронность: ≥ 30% сделок a рядом (±60 с) со сделкой b         0.6
  L5 похожие суммы ставок                                           0.3
  L6 общий владелец EOA                                             1.0
Ребро: L2 или L6, либо сумма весов ≥ 1.0. Серая зона 0.5–1.0 → ручной разбор.
Компоненты связности (union-find) получают общий entity_id.
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

import numpy as np
import polars as pl
from scipy import sparse

from .. import frames
from ..config import Params
from ..db import DB, now_ts


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def update_funders(db: DB, allowlist: tuple = ()) -> None:
    """Первый входящий перевод кошельку = источник финансирования (wallets.funder_wallet)."""
    allow = {a.lower() for a in allowlist}
    rows = db.query("SELECT to_wallet, from_wallet, MIN(ts) AS ts FROM transfers GROUP BY to_wallet, from_wallet")
    first: dict[str, tuple[int, str]] = {}
    for r in rows:
        w = r["to_wallet"]
        if r["from_wallet"] in allow:
            continue
        if w not in first or r["ts"] < first[w][0]:
            first[w] = (r["ts"], r["from_wallet"])
    db.executemany(
        "UPDATE wallets SET funder_wallet=?, first_funded_ts=? WHERE wallet=?",
        [(src, ts, w) for w, (ts, src) in first.items()],
    )


def compute_links(
    trades: pl.DataFrame,
    wallets: pl.DataFrame,
    transfers: pl.DataFrame | None,
    params: Params,
    max_wallets_per_market: int = 300,
) -> dict[tuple[str, str], dict]:
    w = params.sybil_weights
    allow = {a.lower() for a in params.funder_allowlist}
    links: dict[tuple[str, str], dict] = defaultdict(dict)

    # L6 общий владелец, L1 общий источник финансирования
    if not wallets.is_empty():
        for col, code in (("owner_eoa", "L6"), ("funder_wallet", "L1")):
            if col not in wallets.columns:
                continue
            groups = wallets.filter(pl.col(col).is_not_null()).group_by(col).agg(pl.col("wallet")).sort(col).iter_rows()
            for key, members in groups:
                if code == "L1" and str(key).lower() in allow:
                    continue
                members = sorted(members)
                if code == "L1" and len(members) > params.sybil_max_wallets_per_funder:
                    continue  # массовый источник — скорее биржа или мост, чем один владелец
                for a, b in combinations(members, 2):
                    links[_pair(a, b)][code] = {"key": str(key)}
    # L2 прямые переводы
    if transfers is not None and not transfers.is_empty():
        known = set(wallets["wallet"].to_list()) if not wallets.is_empty() else set()
        for fw, tw, amount in transfers.select("from_wallet", "to_wallet", "amount").iter_rows():
            if fw in known and tw in known and fw != tw:
                links[_pair(fw, tw)]["L2"] = {"amount": float(amount)}

    if trades.is_empty():
        return _scored(links, w)

    # L3 Jaccard рынков: пересечения всех пар разом — произведение разреженных матриц «кошелёк × рынок»
    # (популярные рынки, где больше max_wallets_per_market игроков, кандидатов не порождают)
    t = trades.filter(pl.col("market_id").is_not_null()).select("wallet", "market_id").unique(maintain_order=True)
    wuniq, wcode = np.unique(t["wallet"].to_numpy().astype(str), return_inverse=True)
    _, mcode = np.unique(t["market_id"].to_numpy().astype(str), return_inverse=True)
    n_mk = np.bincount(wcode, minlength=len(wuniq))  # рынков у кошелька (все)
    full = sparse.csr_matrix(
        (np.ones(len(t), dtype=np.int32), (wcode, mcode)), shape=(len(wuniq), mcode.max() + 1 if len(t) else 0)
    )
    per_market = np.bincount(mcode, minlength=full.shape[1]) if len(t) else np.zeros(0, int)
    keep = (per_market > 1) & (per_market <= max_wallets_per_market)
    small = full[:, np.flatnonzero(keep)] if len(t) else full
    inter = sparse.triu(small @ small.T, k=1).tocoo()
    if inter.nnz:
        a_i, b_i = inter.row, inter.col
        ok = np.minimum(n_mk[a_i], n_mk[b_i]) >= params.sybil_jaccard_min_markets
        a_i, b_i, cnt = a_i[ok], b_i[ok], inter.data[ok]
        if len(a_i):
            both = np.asarray(full[a_i].multiply(full[b_i]).sum(axis=1)).ravel()  # общие рынки, включая популярные
            jac = cnt / (n_mk[a_i] + n_mk[b_i] - both)
            for ai, bi, j in zip(
                a_i[jac >= params.sybil_jaccard_min],
                b_i[jac >= params.sybil_jaccard_min],
                jac[jac >= params.sybil_jaccard_min],
            ):
                links[_pair(str(wuniq[ai]), str(wuniq[bi]))]["L3"] = {"jaccard": round(float(j), 3)}

    # L4 синхронность сделок по одному токену: пары сделок в окне ±win находятся сдвигами
    # отсортированного массива (векторно), а не перебором в Python
    n_trades = dict(trades.group_by("wallet").len().iter_rows())
    win = params.sybil_sync_window_s
    tt = trades.sort(["token_id", "ts"], maintain_order=True, nulls_last=True)
    _, tok = np.unique(tt["token_id"].fill_null("").to_numpy().astype(str), return_inverse=True)
    tsa = tt["ts"].to_numpy().astype(np.int64)
    wnames, wc = np.unique(tt["wallet"].to_numpy().astype(str), return_inverse=True)
    n = len(tt)
    src, dst = [], []
    alive = np.arange(n - 1) if n > 1 else np.zeros(0, np.int64)
    k = 1
    while len(alive):
        j = alive + k
        close = (tok[j] == tok[alive]) & (tsa[j] - tsa[alive] <= win)
        alive = alive[close]
        j = j[close]
        diff = wc[j] != wc[alive]
        src.append(alive[diff])
        dst.append(j[diff])
        k += 1
        alive = alive[alive + k < n]
    if src:
        i_all = np.concatenate(src + dst)
        j_all = np.concatenate(dst + src)  # пары в обе стороны
        # каждый другой кошелёк учитывается один раз на сделку
        pairs = np.unique(np.stack([i_all, wc[j_all]], axis=1), axis=0) if len(i_all) else np.zeros((0, 2), int)
        if len(pairs):
            ab, counts = np.unique(np.stack([wc[pairs[:, 0]], pairs[:, 1]], axis=1), axis=0, return_counts=True)
            for (a_c, b_c), cnt in zip(ab, counts):
                a, b = str(wnames[a_c]), str(wnames[b_c])
                share = cnt / n_trades[a]
                if share >= params.sybil_sync_share and n_trades[a] >= 5:
                    p = _pair(a, b)
                    prev = links.get(p, {}).get("L4", {}).get("share", 0)
                    links[p]["L4"] = {"share": round(max(prev, share), 3)}

    # L5 похожие суммы — только для пар, уже связанных другими сигналами
    sizes: dict[str, set] = defaultdict(set)
    for wlt, r in zip(trades["wallet"].to_list(), np.round(trades["size_usd"].to_numpy().astype(float), 0)):
        sizes[wlt].add(float(r))
    for a, b in list(links):
        if a in sizes and b in sizes and sizes[a] and sizes[b]:
            common = len(sizes[a] & sizes[b]) / min(len(sizes[a]), len(sizes[b]))
            if common >= params.sybil_size_pattern_share:
                links[(a, b)]["L5"] = {"share": round(common, 3)}

    return _scored(links, w)


def _scored(links: dict, weights: dict) -> dict:
    for sig in links.values():
        sig["_score"] = sum(weights[c] for c in sig if c in weights)
    return dict(links)


def cluster_wallets(links: dict, params: Params) -> tuple[dict[str, str], list[tuple[str, str, float]]]:
    """Возвращает (wallet → entity_id для объединённых кошельков, пары серой зоны)."""
    uf = UnionFind()
    grey = []
    for (a, b), sig in links.items():
        score = sig.get("_score", 0)
        if "L2" in sig or "L6" in sig or score >= params.sybil_link_threshold:
            uf.union(a, b)
        elif score >= params.sybil_grey_threshold:
            grey.append((a, b, score))
    mapping = {}
    for members in uf.groups().values():
        if len(members) > 1:
            eid = "E-" + min(members)
            for m in members:
                mapping[m] = eid
    return mapping, grey


def run_antisybil(db: DB, params: Params, wallets: list[str] | None = None) -> dict:
    """Полный проход: источники финансирования → связи → кластеры → entity_wallets.
    wallets — только эти кошельки (киты из списка пользователя)."""
    update_funders(db, params.funder_allowlist)
    if wallets is not None:
        ph = ",".join("?" for _ in wallets) or "''"
        trades = frames.read(
            db, f"SELECT wallet, token_id, market_id, ts, size_usd FROM trades WHERE wallet IN ({ph})", wallets
        )
        wallets_df = frames.read(
            db, f"SELECT wallet, owner_eoa, funder_wallet FROM wallets WHERE wallet IN ({ph})", wallets
        )
        transfers = frames.read(
            db,
            f"SELECT from_wallet, to_wallet, amount FROM transfers WHERE from_wallet IN ({ph}) OR to_wallet IN ({ph})",
            wallets + wallets,
        )
    else:
        trades = frames.read_history(db, "SELECT wallet, token_id, market_id, ts, size_usd FROM trades")
        wallets_df = frames.read(db, "SELECT wallet, owner_eoa, funder_wallet FROM wallets")
        transfers = frames.read(db, "SELECT from_wallet, to_wallet, amount FROM transfers")
    links = compute_links(trades, wallets_df, transfers, params)
    mapping, grey = cluster_wallets(links, params)
    ts = now_ts()
    scoped = wallets is not None
    scope_list = list(wallets) if wallets is not None else []
    with db.tx():
        if scoped:
            # пересчёт только по своим кошелькам: чужие связи и объединения не трогаем
            for i in range(0, len(scope_list), 400):
                ch = scope_list[i : i + 400]
                ph = ",".join("?" for _ in ch)
                db.execute(f"DELETE FROM wallet_links WHERE wallet_a IN ({ph}) OR wallet_b IN ({ph})", ch + ch)
                db.execute(f"DELETE FROM entity_wallets WHERE wallet IN ({ph})", ch)
        else:
            db.execute("DELETE FROM wallet_links")
        rows = []
        for (a, b), sig in links.items():
            for code, ev in sig.items():
                if code.startswith("_"):
                    continue
                rows.append(
                    {
                        "wallet_a": a,
                        "wallet_b": b,
                        "link_type": code,
                        "strength": params.sybil_weights.get(code, 0),
                        "evidence": json.dumps(ev),
                        "computed_at": ts,
                    }
                )
        db.upsert_many("wallet_links", rows, ["wallet_a", "wallet_b", "link_type"])
        if not scoped:
            db.execute("DELETE FROM entity_wallets")
        db.upsert_many(
            "entity_wallets",
            [
                {"wallet": w, "entity_id": e, "link_reason": "sybil_cluster", "confidence": 1.0}
                for w, e in mapping.items()
            ],
            ["wallet"],
        )
    for a, b, s in grey:
        for wlt in (a, b):
            db.upsert(
                "wallet_flags",
                {"entity_id": wlt, "flag": "sybil_manual_review", "value": s, "computed_at": ts},
                ["entity_id", "flag", "computed_at"],
            )
    return {
        "links": len(links),
        "merged_wallets": len(mapping),
        "entities": len(set(mapping.values())),
        "grey_pairs": len(grey),
    }
