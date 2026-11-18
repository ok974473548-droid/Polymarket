"""Этап 3 «Аналитический движок»: инкрементальный пересчёт и чтение истории через DuckDB."""

import shutil

import pytest

from whalefollow import frames
from whalefollow.db import DB
from whalefollow.positions import assign_entities, build_positions
from whalefollow.scoring.incremental import changed_entities, run_incremental
from whalefollow.scoring.scorer import run_scoring
from whalefollow.scoring.sybil import run_antisybil

from conftest import fast_params
from synthetic import SyntheticWorld, load_world

SMALL = {"seed": 11, "n_events": 200, "n_random": 20, "n_skilled": 3}


def _world_db(url="sqlite://:memory:"):
    w = SyntheticWorld(**SMALL)
    db = DB(url)
    db.init_schema()
    load_world(db, w)
    p = fast_params()
    build_positions(db)
    run_antisybil(db, p)
    assign_entities(db)
    return w, db, p


def _close(a, b, rel=1e-12):
    """Списки чисел равны до rel; пропуски (None/NaN) — только на тех же местах."""
    import math

    def norm(v):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else v

    a, b = [norm(x) for x in a], [norm(x) for x in b]
    return len(a) == len(b) and all(
        (x is None and y is None) or (x is not None and y is not None and math.isclose(x, y, rel_tol=rel))
        for x, y in zip(a, b)
    )


def _latest(db):
    return {
        r["entity_id"]: r["m"]
        for r in db.query("SELECT entity_id, MAX(computed_at) AS m FROM wallet_scores GROUP BY entity_id")
    }


def test_incremental_scores_only_changed_players():
    w, db, p = _world_db()
    first = run_incremental(db, p, now=w.now, full=True)
    n = first["scored"]
    assert n > 10 and first["skipped"] == 0
    again = run_incremental(db, p, now=w.now + 10)
    assert again["scored"] == 0 and again["skipped"] == n  # ничего не изменилось — ничего не считаем

    before = _latest(db)
    e = sorted(before)[0]
    wallet = db.one("SELECT wallet FROM positions WHERE entity_id=? LIMIT 1", (e,))["wallet"]
    tr = db.one("SELECT * FROM trades WHERE wallet=? ORDER BY ts DESC LIMIT 1", (wallet,))
    db.insert_ignore("trades", [{**tr, "trade_id": "new-trade", "ts": tr["ts"] + 60, "size_usd": 77.0}])
    build_positions(db, [wallet])
    changed, _ = changed_entities(db, p)
    assert changed == [e]
    res = run_incremental(db, p, now=w.now + 20)
    assert res["scored"] == 1 and res["skipped"] == n - 1
    after = _latest(db)
    assert after[e] == w.now + 20  # пересчитан только этот игрок
    assert all(after[x] == before[x] for x in before if x != e)

    p2 = fast_params(shrink_k=p.shrink_k + 1)  # другие параметры отбора — пересчитываются все
    assert run_incremental(db, p2, now=w.now + 30)["scored"] == n


def test_full_incremental_equals_plain_scoring():
    w, db, p = _world_db()
    a = run_scoring(db, p, now=w.now).sort("entity_id")
    w2, db2, _ = _world_db()
    run_incremental(db2, p, now=w.now, full=True)
    b = frames.read(db2, "SELECT * FROM wallet_scores ORDER BY entity_id")
    assert a["status"].to_list() == b["status"].to_list()
    assert _close(a["edge_ci_lo"].to_list(), b["edge_ci_lo"].to_list())


@pytest.mark.sqlite_only
def test_duckdb_history_gives_same_scores(tmp_path, monkeypatch):
    """История через DuckDB и обычное чтение дают одинаковые оценки (и одинаковые таблицы)."""
    _, db, p = _world_db(f"sqlite:///{tmp_path}/a.db")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # всё из журнала WAL — в файл, иначе копия неполная
    shutil.copy(tmp_path / "a.db", tmp_path / "b.db")
    q = "SELECT trade_id, ts, wallet, token_id, market_id, side, price, size_shares, size_usd, source FROM trades"
    monkeypatch.setattr(frames, "_duck_ok", None)
    assert frames._duckdb_available(), "расширение DuckDB для SQLite недоступно"
    assert frames.read_history(db, q).equals(frames.read(db, q))
    now = 1_790_000_000
    run_scoring(db, p, now=now)
    monkeypatch.setenv("WF_DUCKDB", "off")
    db_b = DB(f"sqlite:///{tmp_path}/b.db")
    run_scoring(db_b, p, now=now)
    sql = "SELECT * FROM wallet_scores ORDER BY entity_id"
    a, b = frames.read(db, sql), frames.read(db_b, sql)
    assert a.height == b.height > 10
    assert a["status"].to_list() == b["status"].to_list() and a["metrics"].to_list() == b["metrics"].to_list()
    for c in ("edge_ci_lo", "clv_24h", "pvalue", "score"):
        assert _close(a[c].to_list(), b[c].to_list()), c
