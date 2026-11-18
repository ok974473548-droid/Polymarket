import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow.config import Params
from whalefollow.db import DB

from synthetic import EXCHANGE, SyntheticWorld, load_world


def fast_params(**kw) -> Params:
    base = {"bootstrap_b": 1000, "null_sims": 2000, "funder_allowlist": (EXCHANGE,), "min_clusters_category": 15}
    base.update(kw)
    return Params.load(**base)


@pytest.fixture
def db():
    d = DB("sqlite://:memory:")
    d.init_schema()
    return d


@pytest.fixture(scope="session")
def scored_world():
    """Синтетический рынок, прогнанный через загрузку, антисибил, позиции и скоринг (один раз на сессию)."""
    from whalefollow.positions import assign_entities, build_positions
    from whalefollow.scoring.scorer import run_scoring
    from whalefollow.scoring.sybil import run_antisybil

    world = SyntheticWorld(seed=4)
    d = DB("sqlite://:memory:")
    d.init_schema()
    load_world(d, world)
    params = fast_params()
    build_positions(d)
    run_antisybil(d, params)
    assign_entities(d)
    scores = run_scoring(d, params, now=world.now)
    role = {}
    for e in scores["entity_id"]:
        wallet = e[2:] if e.startswith("E-") else e
        role[e] = world.roles.get(wallet, "?")
    scores["role"] = scores["entity_id"].map(role)
    return world, d, params, scores


# ---------------------------------------------------------------- прогон тестов на PostgreSQL
# WF_TEST_PG=postgresql://user:pass@host/db python -m pytest
# Каждая база SQLite из тестов превращается в отдельную схему PostgreSQL (тот же файл — та же схема,
# как у нескольких процессов с общей базой). Тесты, проверяющие именно SQLite, помечены sqlite_only.
_PG = os.environ.get("WF_TEST_PG")
PG_MODE = bool(_PG)
if PG_MODE:
    import atexit
    import uuid

    import psycopg

    _orig_init = DB.__init__
    _schemas: dict[str, str] = {}

    def _pg_init(self, url=None):
        url = url or os.environ.get("WF_DB_URL", "sqlite:///whalefollow.db")
        if url.startswith("sqlite"):
            path = url.split("sqlite://", 1)[-1]
            key = None if ":memory:" in path or not path.strip("/") else path
            schema = _schemas.get(key) if key else None
            if schema is None:
                schema = "t_" + uuid.uuid4().hex[:12]
                with psycopg.connect(_PG, autocommit=True) as c:
                    c.execute(f"CREATE SCHEMA {schema}")
                if key:
                    _schemas[key] = schema
            sep = "&" if "?" in _PG else "?"
            url = f"{_PG}{sep}options=-csearch_path%3D{schema}"
        _orig_init(self, url)

    DB.__init__ = _pg_init

    @atexit.register
    def _drop_schemas():
        try:
            with psycopg.connect(_PG, autocommit=True) as c:
                for (name,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 't\\_%'").fetchall():
                    c.execute(f"DROP SCHEMA {name} CASCADE")
        except Exception:
            pass


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: проверка именно SQLite (на PostgreSQL пропускается)")


def pytest_collection_modifyitems(config, items):
    if PG_MODE:
        skip = pytest.mark.skip(reason="проверка именно SQLite")
        for it in items:
            if "sqlite_only" in it.keywords:
                it.add_marker(skip)
