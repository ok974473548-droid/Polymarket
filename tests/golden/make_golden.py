"""Эталон результатов расчётов (снят на версии 0.15.0, pandas) — для сверки после перехода на polars.

Запуск (из корня проекта): python tests/golden/make_golden.py
Перезаписывать эталон можно только если расчёт меняется сознательно (тест «было → стало» и строка в README).
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from whalefollow.backtest.engine import quick_params, run_backtest, walk_forward  # noqa: E402
from whalefollow.db import DB  # noqa: E402
from whalefollow.positions import assign_entities, build_positions  # noqa: E402
from whalefollow.scoring.scorer import run_scoring  # noqa: E402
from whalefollow.scoring.sybil import run_antisybil  # noqa: E402

from conftest import fast_params  # noqa: E402
from synthetic import SyntheticWorld, load_world  # noqa: E402

DAY = 86400
WORLDS = {
    "main": {"seed": 4},
    "small": {"seed": 11, "n_events": 200, "n_random": 20, "n_skilled": 3},
}
TABLES = {
    "positions": "position_id",
    "wallet_links": "wallet_a, wallet_b, link_type",
    "entity_wallets": "wallet",
    "wallet_scores": "entity_id, computed_at",
    "wallet_category_scores": "entity_id, category, computed_at",
    "wallet_flags": "entity_id, flag, computed_at",
    "watchlist": "entity_id",
}
SKIP_COLS = {"computed_at_wall", "added_ts"}  # время записи по часам, а не по данным


def clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if hasattr(v, "item"):  # numpy-число
        return clean(v.item())
    return v


def _records(df) -> list[dict]:
    """Строки таблицы (pandas — эталон 0.15, polars — новый движок)."""
    return df.to_dicts() if hasattr(df, "to_dicts") else df.to_dict("records")


def _counts(s) -> dict:
    vals = s.to_list() if hasattr(s, "to_list") else list(s)
    return {v: vals.count(v) for v in set(vals)}


def dump_tables(db: DB) -> dict:
    out = {}
    for t, order in TABLES.items():
        keys = [k.strip() for k in order.split(",")]
        # порядок — в Python: PostgreSQL сортирует текст по правилам языка, SQLite — по кодам символов
        rows = sorted(db.query(f"SELECT * FROM {t}"), key=lambda r: tuple(str(r[k]) for k in keys))
        for r in rows:
            for k in list(r):
                if k in SKIP_COLS:
                    r.pop(k)
            if t == "wallet_links":
                r.pop("computed_at", None)
            if t == "wallet_flags" and r.get("flag") == "sybil_manual_review":
                r.pop("computed_at", None)
        out[t] = clean(rows)
    return out


def snapshot(name: str, cfg: dict) -> dict:
    world = SyntheticWorld(**cfg)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, world)
    p = fast_params()
    out: dict = {}
    build_positions(db)
    out["antisybil"] = run_antisybil(db, p)
    assign_entities(db)
    df = run_scoring(db, p, now=world.now)
    out["scoring_status"] = clean(sorted(_counts(df["status"]).items()))
    out["tables"] = dump_tables(db)
    # пересчёт только части игроков (как проверка одного кита / пересчёт китов из списка)
    ents = sorted(df["entity_id"].to_list() if hasattr(df["entity_id"], "to_list") else df["entity_id"])[:3]
    wallets = [r["wallet"] for r in db.query("SELECT DISTINCT wallet FROM positions WHERE entity_id IN (?,?,?)", ents)]
    build_positions(db, wallets)
    sub = run_scoring(db, p, now=world.now + 1, entities=ents, add_new=False)
    out["scoped"] = clean(sorted(_records(sub), key=lambda r: r["entity_id"]))
    q = quick_params(p)
    out["backtest"] = clean(run_backtest(db, q, world.now - 60 * DAY, world.now))
    out["walk_forward"] = clean(walk_forward(db, q, world.now - 120 * DAY, world.now - 60 * DAY))
    return out


if __name__ == "__main__":
    for name, cfg in WORLDS.items():
        data = snapshot(name, cfg)
        path = HERE / f"{name}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True, default=str)
        print(name, path.stat().st_size // 1024, "КБ", data["scoring_status"])
