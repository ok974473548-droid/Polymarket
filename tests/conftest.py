import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow.config import Params  # noqa: E402
from whalefollow.db import DB  # noqa: E402
from synthetic import EXCHANGE, SyntheticWorld, load_world  # noqa: E402


def fast_params(**kw) -> Params:
    base = dict(bootstrap_b=1000, null_sims=2000, funder_allowlist=(EXCHANGE,), min_clusters_category=15)
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
