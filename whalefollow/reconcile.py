"""Сверка нашей реконструкции позиций кита с данными самого Polymarket (/positions, /closed-positions).

Прибыль Polymarket по токену: закрытые — realizedPnl; открытые — realizedPnl + cashPnl.
Наша прибыль: выручка от продаж + оставшиеся акции × оценка − затраты. Оставшиеся акции
оцениваются выплатой (если рынок разрешён) или текущей ценой ИЗ Polymarket — так расхождение
показывает ошибку учёта, а не разницу в ценах.

Вердикт: ok — расхождение ≤ 2 % от вложенного и совпало ≥ 80 % позиций Polymarket;
warn — ≤ 10 % или совпало ≥ 50 %; bad — хуже; no_data — Polymarket ничего не вернул.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from .db import DB, now_ts
from .util import pmap

log = logging.getLogger(__name__)

OK_PCT, WARN_PCT = 0.02, 0.10


def _pm_by_token(open_rows: list[dict], closed_rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "cur": None, "title": None, "open": False})
    for r in closed_rows:
        a = out[str(r.get("asset"))]
        a["pnl"] += float(r.get("realizedPnl") or 0)
        a["title"] = a["title"] or r.get("title")
    for r in open_rows:
        a = out[str(r.get("asset"))]
        a["pnl"] += float(r.get("realizedPnl") or 0) + float(r.get("cashPnl") or 0)
        a["cur"] = float(r["curPrice"]) if r.get("curPrice") is not None else None
        a["title"] = a["title"] or r.get("title")
        a["open"] = True
    return dict(out)


def _ours_by_token(db: DB, wallets: list[str]) -> dict[str, dict]:
    if not wallets:
        return {}
    ph = ",".join("?" for _ in wallets)
    rows = db.query(
        "SELECT p.token_id, SUM(p.cost_usd) AS cost, SUM(p.proceeds_usd) AS proceeds, "
        "SUM(p.remaining_shares) AS rem, MAX(t.payout) AS payout FROM positions p "
        f"LEFT JOIN tokens t ON t.token_id=p.token_id WHERE p.wallet IN ({ph}) GROUP BY p.token_id",
        wallets,
    )
    return {r["token_id"]: r for r in rows}


def reconcile(db: DB, data, entity_id: str, wallets: list[str], now: int | None = None, budget_s: float = 45.0) -> dict:
    now = now or now_ts()
    deadline = time.time() + budget_s  # сверка не должна задерживать проверку игрока

    def fetch(w):
        try:
            return data.positions(w, deadline=deadline), data.closed_positions(w, deadline=deadline)
        except TypeError:  # клиенты без ограничения по времени (тесты, старые)
            return data.positions(w), data.closed_positions(w)

    res = pmap(fetch, wallets, workers=4)
    open_rows, closed_rows = [], []
    for _, r, exc in res:
        if exc:
            log.warning("сверка %s: %s", entity_id, exc)
            continue
        open_rows += r[0]
        closed_rows += r[1]
    pm = _pm_by_token(open_rows, closed_rows)
    ours = _ours_by_token(db, wallets)
    matched, diffs = [], []
    pnl_pm = pnl_ours = invested = 0.0
    for tok, p in pm.items():
        o = ours.get(tok)
        if not o or not o["cost"]:
            continue
        rem = float(o["rem"] or 0)
        mark = o["payout"] if o["payout"] is not None else p["cur"]
        if rem > 1e-6 and mark is None:
            continue  # нечем оценить остаток — позицию не сравниваем
        mine = float(o["proceeds"] or 0) + rem * float(mark or 0) - float(o["cost"])
        matched.append(tok)
        pnl_pm += p["pnl"]
        pnl_ours += mine
        invested += float(o["cost"])
        diffs.append(
            {"title": p["title"], "ours": round(mine, 2), "pm": round(p["pnl"], 2), "diff": round(mine - p["pnl"], 2)}
        )
    n_pm = len(pm)
    if not n_pm:
        verdict, pct = "no_data", None
    else:
        pct = abs(pnl_ours - pnl_pm) / invested if invested else None
        cover = len(matched) / n_pm
        if pct is not None and pct <= OK_PCT and cover >= 0.8:
            verdict = "ok"
        elif (pct is not None and pct <= WARN_PCT) and cover >= 0.5:
            verdict = "warn"
        else:
            verdict = "bad"
    diffs.sort(key=lambda d: -abs(d["diff"]))
    row = {
        "entity_id": entity_id,
        "computed_at": now,
        "n_pm": n_pm,
        "n_ours": len(ours),
        "n_matched": len(matched),
        "pnl_pm": round(pnl_pm, 2),
        "pnl_ours": round(pnl_ours, 2),
        "diff_usd": round(pnl_ours - pnl_pm, 2),
        "diff_pct": round(pct, 4) if pct is not None else None,
        "verdict": verdict,
        "details": json.dumps({"top": diffs[:5], "invested": round(invested, 2)}, ensure_ascii=False),
    }
    db.upsert("wallet_reconcile", row, ["entity_id", "computed_at"])
    return row


def latest(db: DB, entity_id: str) -> dict | None:
    return db.one("SELECT * FROM wallet_reconcile WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (entity_id,))


VERDICT_RU = {
    "ok": "совпадает с Polymarket",
    "warn": "небольшое расхождение с Polymarket",
    "bad": "заметное расхождение с Polymarket — оценка может быть неточной",
    "no_data": "Polymarket не вернул позиции — сверить не удалось",
}


def describe(r: dict | None) -> dict | None:
    """Для экранов: короткий текст и детали."""
    if not r:
        return None
    pct = r.get("diff_pct")
    text = VERDICT_RU.get(r["verdict"], "")
    if pct is not None and r["verdict"] != "no_data":
        text += (
            f": расхождение {pct * 100:.1f}".replace(".", ",")
            + f"% от вложенного по {r['n_matched']} из {r['n_pm']} позиций"
        )
    det = json.loads(r.get("details") or "{}")
    return {
        "verdict": r["verdict"],
        "text": text,
        "pnl_pm": r["pnl_pm"],
        "pnl_ours": r["pnl_ours"],
        "n_matched": r["n_matched"],
        "n_pm": r["n_pm"],
        "top": det.get("top", []),
        "at": r["computed_at"],
    }
