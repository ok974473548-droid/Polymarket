import json

from whalefollow.ingest.polymarket import parse_book, parse_market, parse_price_history, parse_trade, trade_id_for

GAMMA_RESOLVED = {
    "conditionId": "0xabc",
    "question": "Will the central bank cut rates in December?",
    "clobTokenIds": json.dumps(["111", "222"]),
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["1", "0"]),
    "closed": True,
    "endDate": "2026-12-15T00:00:00Z",
    "closedTime": "2026-12-15T12:00:00Z",
    "volumeNum": 125000.5,
    "liquidityNum": 30000,
    "events": [{"id": 77, "title": "Fed December", "tags": [{"slug": "economy"}]}],
}
TRADE_V2 = {
    "proxy_wallet": "0xAbC0000000000000000000000000000000000001",
    "side": "BUY",
    "token_id": "111",
    "condition_id": "0xabc",
    "size": 100.0,
    "price": 0.55,
    "timestamp": 1790000000,
    "transaction_hash": "0xdead",
    "outcome": "Yes",
    "outcome_index": 0,
}


def test_parse_market_resolved():
    m, toks, ev = parse_market(GAMMA_RESOLVED)
    assert m["market_id"] == "0xabc" and m["status"] == "resolved" and m["category"] == "economics"
    assert m["event_id"] == "77" and ev["event_id"] == "77"
    assert {t["token_id"]: t["payout"] for t in toks} == {"111": 1.0, "222": 0.0}
    assert toks[0]["outcome_label"] == "Yes"


def test_parse_market_open_has_no_payout():
    raw = {**GAMMA_RESOLVED, "closed": False, "outcomePrices": json.dumps(["0.55", "0.45"])}
    m, toks, _ = parse_market(raw)
    assert m["status"] == "open" and all(t["payout"] is None for t in toks)


def test_parse_trade_and_stable_id():
    t = parse_trade(TRADE_V2, fetched_ts=1)
    assert t["wallet"] == TRADE_V2["proxy_wallet"].lower()
    assert abs(t["size_usd"] - 55.0) < 1e-9 and t["side"] == "BUY"
    assert t["trade_id"] == trade_id_for(TRADE_V2) == parse_trade(TRADE_V2)["trade_id"]


def test_parse_book_sorted():
    bids, asks = parse_book(
        {
            "bids": [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "5"}],
            "asks": [{"price": "0.52", "size": "3"}, {"price": "0.50", "size": "7"}],
        }
    )
    assert bids[0][0] == 0.45 and asks[0][0] == 0.50


def test_parse_price_history():
    assert parse_price_history({"history": [{"t": 1, "p": 0.5}]}) == [(1, 0.5)]
