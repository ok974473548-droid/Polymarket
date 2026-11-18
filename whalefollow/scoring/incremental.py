"""Инкрементальный пересчёт китов: оцениваются только игроки, у которых изменились входные данные.

Отпечаток игрока — сводка всего, от чего зависит его оценка: позиции (число, затраты, прибыль, закрытые,
последние даты — меняются с новой сделкой или исходом рынка), состав кошельков (антисибил), конвертации
neg-risk и версия параметров отбора. Совпал с прошлым — оценка не пересчитывается.

Сознательно не входят в отпечаток (их подхватывает суточный полный пересчёт): догруженная история цен
(CLV), дата первой активности кошелька и объём рынка (признаки инсайда).

Часть показателей зависит от текущей даты (окно 90 дней, «активен ли игрок»), поэтому раз в сутки
по-прежнему делается полный пересчёт, а между ними — частые быстрые инкрементальные.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..config import Params
from ..db import DB, now_ts

FP_SQL = """
SELECT p.entity_id AS entity_id, COUNT(*) AS n, SUM(p.cost_usd) AS cost, SUM(COALESCE(p.pnl_usd, 0)) AS pnl,
       SUM(CASE WHEN p.closed_ts IS NOT NULL THEN 1 ELSE 0 END) AS n_closed, MAX(p.opened_ts) AS last_open,
       MAX(COALESCE(p.closed_ts, 0)) AS last_close, SUM(p.n_trades) AS n_trades,
       COUNT(DISTINCT p.wallet) AS n_wallets, MIN(p.wallet) AS w_min, MAX(p.wallet) AS w_max
FROM positions p {where} GROUP BY p.entity_id
"""


def fingerprints(db: DB, params: Params, entities: list[str] | None = None) -> dict[str, str]:
    """{игрок: отпечаток входных данных}."""
    where, args = "", []
    if entities:
        where = f"WHERE p.entity_id IN ({','.join('?' for _ in entities)})"
        args = list(entities)
    conv = {
        r["wallet"]: r["n"]
        for r in db.query("SELECT wallet, COUNT(*) AS n FROM activities WHERE type='CONVERSION' GROUP BY wallet")
    }
    wallets: dict[str, list[str]] = {}
    for r in db.query(f"SELECT DISTINCT p.entity_id, p.wallet FROM positions p {where}", args):
        wallets.setdefault(r["entity_id"], []).append(r["wallet"])
    version = params.version()
    out = {}
    for r in db.query(FP_SQL.format(where=where), args):
        e = r["entity_id"]
        data: dict[str, Any] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()}
        data["conversions"] = sum(conv.get(w, 0) for w in wallets.get(e, []))
        data["params"] = version
        out[e] = hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    return out


def changed_entities(db: DB, params: Params, entities: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """(игроки с изменившимися данными, их новые отпечатки)."""
    fps = fingerprints(db, params, entities)
    stored = {r["entity_id"]: r["fingerprint"] for r in db.query("SELECT entity_id, fingerprint FROM score_inputs")}
    changed = sorted(e for e, fp in fps.items() if stored.get(e) != fp)
    return changed, fps


def remember(db: DB, fps: dict[str, str], entities: list[str]) -> None:
    ts = now_ts()
    db.upsert_many(
        "score_inputs",
        [{"entity_id": e, "fingerprint": fps[e], "scored_at": ts} for e in entities if e in fps],
        ["entity_id"],
    )


def run_incremental(
    db: DB,
    params: Params,
    now: int | None = None,
    entities: list[str] | None = None,
    full: bool = False,
    add_new: bool = False,
) -> dict:
    """Пересчитать оценки только изменившихся игроков (full=True — всех из entities или всех вообще).
    Позиции и антисибил к этому моменту уже обновлены (это быстро); дорогая часть — оценка — только по нужным."""
    from .scorer import run_scoring

    t0 = time.perf_counter()
    changed, fps = changed_entities(db, params, entities)
    todo = sorted(fps) if full else changed
    if entities is not None:
        allowed = set(entities)
        todo = [e for e in todo if e in allowed]
    status: dict = {}
    if todo:
        # полный пересчёт без ограничения списка — прежним путём (как ночной пересчёт до 0.16)
        scope = None if full and entities is None else todo
        df = run_scoring(db, params, now=now, entities=scope, add_new=add_new)
        if df.height:
            status = dict(df["status"].value_counts().iter_rows())
        remember(db, fps, todo)
    return {
        "scored": len(todo),
        "skipped": len(fps) - len(todo),
        "full": full,
        "status": status,
        "seconds": round(time.perf_counter() - t0, 2),
    }
