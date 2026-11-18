"""Скоринг на синтетическом рынке: находит заложенный навык и не находит случайный (критерий этапа 3 ТЗ)."""

import polars as pl

from whalefollow.config import Params
from whalefollow.scoring.sybil import cluster_wallets, compute_links


def test_random_players_are_never_approved(scored_world):
    _, _, _, s = scored_world
    assert s.filter((pl.col("role") == "random") & (pl.col("status") == "approved")).height == 0


def test_some_skilled_players_are_approved(scored_world):
    _, _, _, s = scored_world
    assert s.filter(pl.col("role").is_in(["skilled", "sybil"]) & (pl.col("status") == "approved")).height >= 2


def test_bad_classes_excluded(scored_world):
    _, _, _, s = scored_world

    def classes(role):
        return set(s.filter(pl.col("role") == role)["class"].to_list())

    assert classes("maker") == {"maker_arb"}
    assert classes("grinder") == {"favorite_grinder"}
    assert classes("insider") == {"insider_suspect"}
    bad = s.filter(pl.col("role").is_in(["maker", "grinder", "insider"]))
    assert bad.height and (bad["status"] == "excluded").all()


def test_sybil_wallets_merged_into_one_entity(scored_world):
    world, db, _, s = scored_world
    sybils = [w for w, r in world.roles.items() if r == "sybil"]
    ents = {db.scalar("SELECT entity_id FROM entity_wallets WHERE wallet=?", (w,)) for w in sybils}
    assert len(ents) == 1 and None not in ents
    assert s.filter(pl.col("role") == "sybil").height == 1


def test_skilled_have_higher_clv_than_random(scored_world):
    _, _, _, s = scored_world

    def clv(role):
        return s.filter(pl.col("role") == role)["clv_24h"].fill_nan(None).mean()

    assert clv("skilled") > clv("random")


def test_watchlist_built_from_approved(scored_world):
    _, db, _, s = scored_world
    wl = {r["entity_id"] for r in db.query("SELECT entity_id FROM watchlist WHERE mode='copy'")}
    assert wl == set(s.filter(pl.col("status") == "approved")["entity_id"].to_list())


def test_exchange_funder_does_not_link_wallets():
    p = Params(funder_allowlist=("0xexch",))
    wallets = pl.DataFrame(
        {
            "wallet": ["a", "b", "c", "d"],
            "owner_eoa": [None] * 4,
            "funder_wallet": ["0xexch", "0xexch", "0xown", "0xown"],
        },
        schema_overrides={"owner_eoa": pl.Utf8},
    )
    links = compute_links(pl.DataFrame(), wallets, None, p)
    assert ("a", "b") not in links and links[("c", "d")]["L1"]
    mapping, grey = cluster_wallets(links, p)
    assert mapping == {} and grey and grey[0][:2] == ("c", "d")  # один L1 = серая зона, не склейка


def test_direct_transfer_merges():
    p = Params()
    wallets = pl.DataFrame(
        {"wallet": ["a", "b"], "owner_eoa": [None, None], "funder_wallet": [None, None]},
        schema_overrides={"owner_eoa": pl.Utf8, "funder_wallet": pl.Utf8},
    )
    tr = pl.DataFrame({"from_wallet": ["a"], "to_wallet": ["b"], "amount": [100.0]})
    mapping, _ = cluster_wallets(compute_links(pl.DataFrame(), wallets, tr, p), p)
    assert mapping == {"a": "E-a", "b": "E-a"}
