"""Живой интерфейс: поток событий (SSE), темы как в меню Polymarket."""
import http.client
import json
import threading
import time

from conftest import fast_params
from whalefollow import categories
from whalefollow.api.server import serve
from whalefollow.events import bus
from whalefollow.ingest import pipeline
from whalefollow.ingest.polymarket import normalize_category, parse_market
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services


def test_topics_follow_polymarket_menu():
    assert categories.KEYS == ["politics", "sports", "esports", "crypto", "finance", "culture", "mentions", "weather",
                               "economics", "tech"]
    tag = lambda *xs: {"tags": [{"slug": x} for x in xs]}                         # noqa: E731
    assert normalize_category(tag("sports", "esports", "counter-strike")) == "esports"   # узкая тема важнее
    assert normalize_category(tag("finance", "stocks")) == "finance"              # раньше было «экономика»
    assert normalize_category(tag("fed-rates")) == "economics"
    assert normalize_category({"events": [{"tags": [{"slug": "mention-markets"}]}]}) == "mentions"
    assert normalize_category(tag("hurricanes")) == "weather"
    assert normalize_category({"category": "Pop Culture"}) == "culture"
    assert normalize_category(tag("something-new")) == "other"
    svc_cats = Services.LB_CATS
    assert set(svc_cats) == {"overall", *categories.KEYS} and svc_cats["esports"] == "ESPORTS"


def test_topic_migration_keeps_markets_switched_on(db):
    db.set_setting("categories", ["economics", "politics", "sports", "other"])
    db.set_setting("migrated_other_category", True)
    Scheduler(db, fast_params(), None, None, None, None, clock=lambda: 1)
    assert db.get_setting("categories") == ["economics", "politics", "sports", "other", "finance", "esports",
                                            "mentions", "weather"]
    db.set_setting("categories", ["politics"])                                    # второй запуск ничего не меняет
    Scheduler(db, fast_params(), None, None, None, None, clock=lambda: 1)
    assert db.get_setting("categories") == ["politics"]


def test_recategorize_job_rereads_markets_once(db):
    raw = {"conditionId": "0xm1", "question": "Will NVDA close above $200?", "clobTokenIds": json.dumps(["a", "b"]),
           "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(["0.5", "0.5"]),
           "tags": [{"slug": "finance"}, {"slug": "stocks"}]}
    m, toks, _ = parse_market(raw)
    db.upsert("markets", {**m, "category": "economics"}, ["market_id"])          # записано старой версией
    db.upsert_many("tokens", toks, ["token_id"])

    class G:
        calls = 0

        def markets_by_ids(self, ids):
            G.calls += 1
            return [raw]
    sch = Scheduler(db, fast_params(), G(), None, None, None, clock=lambda: 1)
    assert sch.job_recategorize() == 1
    assert db.scalar("SELECT category FROM markets WHERE market_id='0xm1'") == "finance"
    assert sch.job_recategorize() is None and G.calls == 1


def _pending_signal(db, sid, token="tok1"):
    db.upsert("signals", {"signal_id": sid, "ts": int(time.time()), "entity_id": "0xw", "wallet": "0xw", "trade_id": sid,
                          "token_id": token, "market_id": "m", "whale_price": 0.5, "whale_size_usd": 1000.0,
                          "kind": "entry", "status": "pending", "expires_ts": int(time.time()) + 60}, ["signal_id"])


def test_event_stream_pushes_changes_and_prices(db):
    svc = Services(db, fast_params())
    stop = threading.Event()
    gen = svc.event_stream(stop.is_set, tick=0.05)
    assert next(gen)[0] == "hello"
    got = []

    def reader():
        for ev in gen:
            got.append(ev)
            if len([e for e in got if e[0] in ("change", "price")]) >= 2:
                stop.set()
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t0 = time.time()
    _pending_signal(db, "s_new")
    pipeline.store_book(db, "tok1", [(0.51, 100)], [(0.53, 80)], int(time.time()))
    bus.publish("signal", signal_id="s_new")
    th.join(5)
    kinds = {e[0]: e[1] for e in got}
    assert kinds["change"]["new_pending"] == ["s_new"] and "signals" in kinds["change"]["what"]
    assert kinds["price"] == {"token_id": "tok1", "bid": 0.51, "ask": 0.53, "live": False}
    assert time.time() - t0 < 3


def test_sse_endpoint_over_http(db):
    svc = Services(db, fast_params())
    httpd = serve(svc, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
        assert r.readline() == b"retry: 2000\n"
        r.readline()
        assert r.readline() == b"event: hello\n"
        _pending_signal(db, "s_http")
        bus.publish("signal", signal_id="s_http")
        lines = []
        while not any(b"s_http" in x for x in lines):
            lines.append(r.readline())
        assert b"event: change\n" in lines
        c.close()
    finally:
        httpd.shutdown()
