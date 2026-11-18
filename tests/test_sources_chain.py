"""Шаг 4: переводы USDC из Polygon (антисибил), Telegram-каналы, Reddit."""
import pytest

from conftest import fast_params
from whalefollow.db import DB
from whalefollow.estimator.sources import build_sources, normalize_channel, parse_reddit, parse_tme
from whalefollow.ingest.chain import USDC_E, ChainError, ExplorerClient, parse_transfers, sync_transfers
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services

TME = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="fednews/101">
<div class="tgme_widget_message_text js-message_text" dir="auto">Fed officials <b>signal</b> a December cut<br/>details inside</div>
<div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/fednews/101">
<time datetime="2026-11-17T10:00:00+00:00" class="time">10:00</time></a></div></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="fednews/102">
<div class="tgme_widget_message_photo_wrap"></div>
<a class="tgme_widget_message_date"><time datetime="2026-11-17T11:00:00+00:00">11:00</time></a></div></div>"""

REDDIT = {"data": {"children": [{"data": {"title": "Fed cut odds?", "subreddit": "economics",
                                          "permalink": "/r/economics/comments/abc/", "created_utc": 1790000000}}]}}


def test_normalize_channel():
    assert normalize_channel("@fednews") == "fednews"
    assert normalize_channel("https://t.me/s/fednews?q=x") == "fednews"
    assert normalize_channel("t.me/fednews/101") == "fednews"
    assert normalize_channel("бла бла") is None and normalize_channel("@ab") is None


def test_parse_tme_and_reddit():
    items = parse_tme(TME, "fednews")
    assert len(items) == 1                                    # сообщение без текста (только фото) пропущено
    it = items[0]
    assert it.text_hash and it.source == "@fednews" and it.source_type == "telegram"
    assert it.title == "Fed officials signal a December cut details inside"
    assert it.url == "https://t.me/fednews/101" and it.ts_pub == 1794909600
    r = parse_reddit(REDDIT)
    assert r[0].source == "r/economics" and r[0].url.endswith("/r/economics/comments/abc/")


def test_build_sources_from_settings():
    news, fc = build_sources({"google_news": False, "manifold": True, "reddit": True}, ["@fednews", "bad name"])
    names = [type(s).__name__ for s in news]
    assert names == ["RedditSource", "TelegramChannelSource"] and news[1].channels == ["fednews"]
    assert [type(s).__name__ for s in fc] == ["ManifoldSource"]


def test_parse_transfers_filters_token_and_scales():
    rows = [{"hash": "0x1", "logIndex": "3", "from": "0xF", "to": "0xA", "value": "2500000", "tokenDecimal": "6",
             "timeStamp": "100", "contractAddress": USDC_E.upper()},
            {"hash": "0x2", "from": "0xF", "to": "0xA", "value": "1", "tokenDecimal": "18", "timeStamp": "101",
             "contractAddress": "0xdeadbeef"}]
    out = parse_transfers(rows)
    assert len(out) == 1 and out[0]["amount"] == 2.5 and out[0]["from_wallet"] == "0xf"


class _Resp:
    def __init__(self, payload, code=200):
        self.payload, self.status_code = payload, code

    def json(self):
        return self.payload


class _Sess:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return self.pages.pop(0)


def test_explorer_client_pagination_and_errors():
    row = {"hash": "0x1", "from": "0xf", "to": "0xa", "value": "1000000", "tokenDecimal": "6", "timeStamp": "1",
           "contractAddress": USDC_E}
    s = _Sess([_Resp({"status": "1", "result": [row, row]}), _Resp({"status": "1", "result": [row]})])
    c = ExplorerClient("etherscan", "KEY", session=s, min_interval_s=0)
    assert len(c.token_transfers("0xa", USDC_E, page_size=2)) == 3
    assert s.calls[0]["chainid"] == 137 and s.calls[0]["apikey"] == "KEY" and s.calls[1]["page"] == 2
    c2 = ExplorerClient("blockscout", None, session=_Sess([_Resp({"status": "0", "message": "No transactions found",
                                                                  "result": []})]), min_interval_s=0)
    assert c2.token_transfers("0xa", USDC_E) == []
    c3 = ExplorerClient("etherscan", "BAD", session=_Sess([_Resp({"status": "0", "message": "NOTOK",
                                                                  "result": "Invalid API Key"})]), min_interval_s=0)
    with pytest.raises(ChainError):
        c3.token_transfers("0xa", USDC_E)


def test_from_env(monkeypatch):
    for k in ("WF_CHAIN_PROVIDER", "WF_ETHERSCAN_API_KEY", "WF_BLOCKSCOUT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert ExplorerClient.from_env() is None
    monkeypatch.setenv("WF_CHAIN_PROVIDER", "blockscout")
    assert ExplorerClient.from_env().provider == "blockscout"
    monkeypatch.delenv("WF_CHAIN_PROVIDER")
    monkeypatch.setenv("WF_ETHERSCAN_API_KEY", "K")
    assert ExplorerClient.from_env().provider == "etherscan"


class FakeChain:
    """Кошельки a и b пополнены одним личным кошельком f, и a переводил b."""
    provider = "fake"

    def __init__(self):
        self.asked = []

    def token_transfers(self, address, token, **_):
        self.asked.append(address)
        if token != USDC_E:
            return []
        rows = {"0xa": [("0xf", "0xa", 1), ("0xa", "0xb", 2)], "0xb": [("0xf", "0xb", 3)]}.get(address, [])
        return [{"hash": f"0x{i}{address}", "from": f, "to": t, "value": "5000000", "tokenDecimal": "6",
                 "timeStamp": str(ts), "contractAddress": USDC_E} for i, (f, t, ts) in enumerate(rows)]


def test_transfers_merge_owner_wallets():
    db = DB("sqlite://:memory:")
    db.init_schema()
    for w in ("0xa", "0xb", "0xc"):
        db.upsert("wallets", {"wallet": w, "owner_eoa": None, "is_proxy": 1, "first_seen_ts": 1,
                              "first_funded_ts": None, "funder_wallet": None}, ["wallet"])
    chain = FakeChain()
    res = sync_transfers(db, chain, ["0xA", "0xb", "0xc"])
    assert res["wallets"] == 3 and res["transfers"] == 3
    assert sync_transfers(db, chain, ["0xa", "0xb"])["wallets"] == 0        # повторно не запрашиваем
    out = run_antisybil(db, fast_params(funder_allowlist=()))
    assert out["merged_wallets"] == 2
    ent = {r["wallet"]: r["entity_id"] for r in db.query("SELECT * FROM entity_wallets")}
    assert ent["0xa"] == ent["0xb"] and "0xc" not in ent


def test_settings_telegram_channels_validation():
    db = DB("sqlite://:memory:")
    db.init_schema()
    svc = Services(db, fast_params())
    r = svc.update_settings({"telegram_channels": "@fednews, https://t.me/s/polymarket_news"})
    assert not r["errors"] and r["settings"]["telegram_channels"] == ["fednews", "polymarket_news"]
    assert "telegram_channels" in svc.update_settings({"telegram_channels": ["@ok_channel", "плохое имя"]})["errors"]
    assert svc.get_settings()["chain"]["available"] in (True, False)
