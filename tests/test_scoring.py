"""Скоринг на синтетическом рынке: находит заложенный навык и не находит случайный (критерий этапа 3 ТЗ)."""

import pandas as pd

from whalefollow.config import Params
from whalefollow.scoring.sybil import cluster_wallets, compute_links


def test_random_players_are_never_approved(scored_world):
    _, _, _, s = scored_world
    assert (s[s.role == "random"]["status"] == "approved").sum() == 0


def test_some_skilled_players_are_approved(scored_world):
    _, _, _, s = scored_world
    assert (s[s.role.isin(["skilled", "sybil"])]["status"] == "approved").sum() >= 2


def test_bad_classes_excluded(scored_world):
    _, _, _, s = scored_world
    by_role = s.set_index("role")
    assert set(s[s.role == "maker"]["class"]) == {"maker_arb"}
    assert set(s[s.role == "grinder"]["class"]) == {"favorite_grinder"}
    assert set(s[s.role == "insider"]["class"]) == {"insider_suspect"}
    assert (s[s.role.isin(["maker", "grinder", "insider"])]["status"] == "excluded").all()
    assert len(by_role) == len(s)


def test_sybil_wallets_merged_into_one_entity(scored_world):
    world, db, _, s = scored_world
    sybils = [w for w, r in world.roles.items() if r == "sybil"]
    ents = {db.scalar("SELECT entity_id FROM entity_wallets WHERE wallet=?", (w,)) for w in sybils}
    assert len(ents) == 1 and None not in ents
    assert (s.role == "sybil").sum() == 1


def test_skilled_have_higher_clv_than_random(scored_world):
    _, _, _, s = scored_world
    assert s[s.role == "skilled"]["clv_24h"].mean() > s[s.role == "random"]["clv_24h"].mean()


def test_watchlist_built_from_approved(scored_world):
    _, db, _, s = scored_world
    wl = {r["entity_id"] for r in db.query("SELECT entity_id FROM watchlist WHERE mode='copy'")}
    assert wl == set(s[s.status == "approved"]["entity_id"])


def test_exchange_funder_does_not_link_wallets():
    p = Params(funder_allowlist=("0xexch",))
    wallets = pd.DataFrame(
        {
            "wallet": ["a", "b", "c", "d"],
            "owner_eoa": [None] * 4,
            "funder_wallet": ["0xexch", "0xexch", "0xown", "0xown"],
        }
    )
    links = compute_links(pd.DataFrame(), wallets, pd.DataFrame(columns=["from_wallet", "to_wallet", "amount"]), p)
    assert ("a", "b") not in links and links[("c", "d")]["L1"]
    mapping, grey = cluster_wallets(links, p)
    assert mapping == {} and grey and grey[0][:2] == ("c", "d")  # один L1 = серая зона, не склейка


def test_direct_transfer_merges():
    p = Params()
    wallets = pd.DataFrame({"wallet": ["a", "b"], "owner_eoa": [None, None], "funder_wallet": [None, None]})
    tr = pd.DataFrame({"from_wallet": ["a"], "to_wallet": ["b"], "amount": [100.0]})
    mapping, _ = cluster_wallets(compute_links(pd.DataFrame(), wallets, tr, p), p)
    assert mapping == {"a": "E-a", "b": "E-a"}
