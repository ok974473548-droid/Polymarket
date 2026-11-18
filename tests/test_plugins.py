"""Плагины: реестр и загрузка, источники, уведомления, стратегии (живой фильтр и бэктест), API экрана."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from whalefollow import plugins
from whalefollow.backtest.whale_sim import simulate_whale
from whalefollow.db import DB
from whalefollow.estimator.sources import Forecast, NewsItem
from whalefollow.plugins import Candidate, Notice, NotifierPlugin, SourcePlugin, StrategyPlugin, Verdict
from whalefollow.plugins.builtin.notifiers import Discord, Email
from whalefollow.plugins.notify import NotificationHub
from whalefollow.plugins.registry import Registry
from whalefollow.plugins.strategy import backtest, gate

from conftest import approved_ids, fast_params
from synthetic import SyntheticWorld, load_world

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "no-plugins"))
    plugins.reset_registry()
    yield
    plugins.reset_registry()


# ---------------------------------------------------------------- реестр
def test_builtin_plugins_and_legacy_switches(db):
    reg = plugins.registry()
    names = {p.name: p.kind for p in reg.plugins.values()}
    assert {"google_news", "reddit", "telegram_channels", "manifold", "kalshi", "bookmakers"} <= {
        n for n, k in names.items() if k == "source"
    }
    assert {"telegram", "push", "discord", "email"} <= {n for n, k in names.items() if k == "notifier"}
    assert {"copy_whale", "consensus", "rotation_form"} <= {n for n, k in names.items() if k == "strategy"}
    s = plugins.user_settings(db)
    # по умолчанию — как до плагинов: стратегия «Копировать кита», прежние источники
    assert reg.is_enabled(reg.get("copy_whale"), s) and not reg.is_enabled(reg.get("consensus"), s)
    assert reg.is_enabled(reg.get("kalshi"), s) and not reg.is_enabled(reg.get("reddit"), s)
    # выключение старого источника пишется в прежнюю настройку — экран «Настройки» видит то же
    reg.set_enabled(db, "kalshi", False)
    assert db.get_setting("news_sources")["kalshi"] is False
    reg.set_enabled(db, "consensus", True)
    assert db.get_setting("plugins") == {"consensus": True}
    s = plugins.user_settings(db)
    assert not reg.is_enabled(reg.get("kalshi"), s) and reg.is_enabled(reg.get("consensus"), s)


def test_plugin_file_dropped_into_folder(tmp_path, db):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "big.py").write_text(
        "from whalefollow.plugins import StrategyPlugin, Verdict\n"
        "class Big(StrategyPlugin):\n"
        "    name = 'big_only'\n"
        "    title = 'Только крупные'\n"
        "    def accept(self, c):\n"
        "        return Verdict(c.size_usd >= 1000, 'размер')\n"
        "PLUGINS = [Big()]\n",
        encoding="utf-8",
    )
    (folder / "broken.py").write_text("import nonexistent_module_xyz\n", encoding="utf-8")
    (folder / "_private.py").write_text("raise SystemExit\n", encoding="utf-8")
    reg = Registry()
    reg.load_builtin()
    reg.load_dir(folder)
    assert reg.get("big_only") is not None and reg.get("big_only").origin == "файл big.py"
    assert [e["origin"] for e in reg.load_errors] == ["файл broken.py"]
    assert "nonexistent_module_xyz" in reg.load_errors[0]["error"]


def test_example_plugins_load(db):
    reg = Registry()
    reg.load_dir(EXAMPLES)
    assert not reg.load_errors, reg.load_errors
    assert {p.name: p.kind for p in reg.plugins.values()} == {
        "example_rss": "source",
        "example_journal": "notifier",
        "example_big_bets": "strategy",
    }
    ctx = plugins.context(db)
    assert "EXAMPLE_RSS_URL" in reg.get("example_rss").available(ctx)


def test_name_clash_keeps_first():
    reg = Registry()
    reg.load_builtin()

    class Fake(StrategyPlugin):
        name = "copy_whale"

    assert not reg.register(Fake(), "файл x.py")
    assert reg.get("copy_whale").origin == "встроенный" and "уже есть" in reg.load_errors[-1]["error"]


# ---------------------------------------------------------------- источники
class _Src(SourcePlugin):
    name = "test_src"
    title = "Тестовый источник"
    fail = False

    def news(self, ctx, query, since_ts):
        if self.fail:
            raise RuntimeError("сеть недоступна")
        return [NewsItem("test", "media", since_ts + 1, f"Новость: {query}", "https://example.org/a")]

    def forecasts(self, ctx, question, market=None):
        return [Forecast(source="test", title=question, url="https://example.org/f", probability=0.61)]


def test_research_sources_use_enabled_plugins(db):
    reg = plugins.registry()
    src = _Src()
    reg.register(src, "тест")
    for n in ("google_news", "manifold", "kalshi", "bookmakers"):
        reg.set_enabled(db, n, False)
    news, fc = plugins.research_sources(db)
    assert len(news) == 1 and len(fc) == 1  # только наш источник: остальные выключены
    assert news[0].search("Will X?", 100)[0].title == "Новость: Will X?"
    assert fc[0].forecasts("Will X?", market={})[0].probability == 0.61
    assert plugins.status(db, "test_src")["calls"] >= 1
    src.fail = True
    with pytest.raises(RuntimeError):
        news[0].search("Will X?", 100)
    st = plugins.status(db, "test_src")
    assert st["errors"] == 1 and "сеть недоступна" in st["error"]
    reg.set_enabled(db, "test_src", False)
    assert plugins.research_sources(db) == ([], [])


def test_sync_jobs_keep_old_names_and_skip_without_client(db):
    jobs = {name: (interval, fn) for name, interval, fn in plugins.sync_jobs(db, {})}
    assert jobs["kalshi"][0] == 1800 and "odds" in jobs
    assert jobs["kalshi"][1]() is None  # без клиента (не worker) — без обращений к сети


# ---------------------------------------------------------------- уведомления
class _Box(NotifierPlugin):
    name = "test_box"
    title = "Ящик"

    def __init__(self):
        self.got: list[Notice] = []
        self.fail = False

    def send(self, ctx, notice):
        if self.fail:
            raise ConnectionError("нет связи")
        self.got.append(notice)


def test_notification_hub_routes_by_settings(db, monkeypatch):
    monkeypatch.setenv("WF_PUBLIC_URL", "https://wf.example")
    box = _Box()
    plugins.registry().register(box, "тест")
    hub = NotificationHub.attach_to(db)
    db.notify("signal", "Новый сигнал", "Will X?", "s_1")
    db.notify("whale_exit", "Кит вышел", "", "s_1")
    hub.wait()
    assert [(n.kind, n.url) for n in box.got] == [
        ("signal", "https://wf.example/#/signal/s_1"),
        ("whale_exit", "https://wf.example/#/positions/open"),
    ]
    db.set_setting("notify", {"signals": False})  # «О чём» — общее для всех каналов
    db.notify("signal", "Ещё сигнал", "", "s_2")
    plugins.registry().set_enabled(db, "test_box", False)
    db.notify("whale_exit", "Кит вышел", "", "s_2")
    hub.wait()
    assert len(box.got) == 2
    plugins.registry().set_enabled(db, "test_box", True)
    box.fail = True
    db.notify("whale_exit", "Кит вышел", "", "s_3")
    hub.wait()
    assert "нет связи" in plugins.status(db, "test_box")["error"]


class _Resp:
    def __init__(self, code=204):
        self.status_code = code


class _Sess:
    def __init__(self, code=204):
        self.code, self.calls = code, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return _Resp(self.code)


def test_discord_and_email(db, monkeypatch):
    monkeypatch.setenv("WF_DISCORD_WEBHOOK", "https://discord.example/hook")
    s = _Sess()
    d = Discord(session=s)
    ctx = plugins.context(db)
    assert d.available(ctx) is None
    d.send(ctx, Notice("signal", "Новый сигнал", "Will X?", "s_1", "https://wf/#/signal/s_1"))
    assert s.calls[0][0] == "https://discord.example/hook" and "**Новый сигнал**" in s.calls[0][1]["content"]
    with pytest.raises(RuntimeError):
        Discord(session=_Sess(500)).send(ctx, Notice("signal", "x"))

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent.append(("tls",))

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(("msg", msg["To"], msg["Subject"]))

    monkeypatch.setattr("whalefollow.plugins.builtin.notifiers.smtplib.SMTP", FakeSMTP)
    e = Email()
    assert "WF_SMTP_HOST" in e.available(ctx)
    monkeypatch.setenv("WF_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("WF_EMAIL_TO", "me@example.org")
    monkeypatch.setenv("WF_SMTP_USER", "bot@example.org")
    assert e.available(ctx) is None
    assert not plugins.registry().is_enabled(plugins.registry().get("email"), ctx.settings)  # почта — по флажку
    e.send(ctx, Notice("whale_exit", "Кит вышел", "продал всё", "s_1", "https://wf/#/positions"))
    assert sent == [
        ("connect", "smtp.example", 587),
        ("tls",),
        ("login", "bot@example.org"),
        ("msg", "me@example.org", "WhaleFollow: Кит вышел"),
    ]


# ---------------------------------------------------------------- стратегии
def _trade(db, wallet, token, ts, side="BUY", usd=100.0, price=0.5):
    db.upsert(
        "trades",
        {
            "trade_id": f"{wallet}-{token}-{ts}-{side}",
            "ts": ts,
            "wallet": wallet,
            "market_id": "m1",
            "token_id": token,
            "side": side,
            "price": price,
            "size_shares": usd / price,
            "size_usd": usd,
            "ts_fetched": ts,
        },
        ["trade_id"],
    )


def _watch(db, entity, mode="copy"):
    db.upsert(
        "watchlist",
        {"entity_id": entity, "mode": mode, "added_by": "user", "added_ts": 1, "categories_off": "[]"},
        ["entity_id"],
    )


def test_consensus_counts_distinct_whales_before_now(db):
    a, b, c = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "c3" * 20
    _watch(db, a)
    _watch(db, b, "watch")  # следим, но не копируем — тоже голос
    _trade(db, a, "tok", 10_000)
    _trade(db, c, "tok", 10_100)  # не из списка — не считается
    _trade(db, b, "tok", 10_000 + 5 * 86400)  # через 5 дней — вне окна 3 дня для первой покупки
    cons = plugins.registry().get("consensus")
    first = Candidate(db=db, ts=10_000, entity_id=a, wallet=a, token_id="tok", market_id="m1", price=0.5, size_usd=100)
    assert not cons.accept(first).ok  # один кит
    later = Candidate(
        db=db, ts=10_000 + 2 * 86400, entity_id=a, wallet=a, token_id="tok", market_id="m1", price=0.5, size_usd=100
    )
    _trade(db, b, "tok", 10_000 + 86400)
    assert cons.accept(later).ok
    # будущее не видно: покупка b через сутки не влияет на решение в момент первой покупки
    assert not cons.accept(first).ok


def test_rotation_strategy_uses_schedule_without_lookahead(db):
    rot = plugins.registry().get("rotation_form")
    week = 7 * 86400
    db.set_state("rotation_schedule", {"items": [{"ts": 100 * week, "ids": ["e1"]}, {"ts": 101 * week, "ids": ["e2"]}]})

    def cand(ts, e):
        return Candidate(db=db, ts=ts, entity_id=e, wallet=e, token_id="t", market_id="m", price=0.5, size_usd=1)

    assert not rot.accept(cand(99 * week, "e1")).ok  # до первого пересмотра — данных нет
    assert rot.accept(cand(100 * week + 5, "e1")).ok
    assert not rot.accept(cand(100 * week + 5, "e2")).ok  # e2 в форме только со следующего пересмотра
    assert rot.accept(cand(101 * week + 5, "e2")).ok
    assert not rot.accept(cand(111 * week, "e2")).ok  # пересмотр слишком старый


def test_gate_default_takes_everything_and_reports_errors(db):
    sig = {
        "ts": 1,
        "entity_id": "e",
        "wallet": "w",
        "token_id": "t",
        "market_id": "m",
        "whale_price": 0.4,
        "whale_size_usd": 50,
    }
    g = gate(db, sig, {}, plugins.user_settings(db))
    assert g["ok"] and g["by"] == ["copy_whale"]

    class Boom(StrategyPlugin):
        name = "boom"
        default_enabled = True

        def accept(self, c):
            raise ValueError("сломалась")

    plugins.registry().register(Boom(), "тест")
    plugins.registry().set_enabled(db, "copy_whale", False)
    g = gate(db, sig, {}, plugins.user_settings(db))
    assert not g["ok"] and "сломалась" in g["verdicts"]["boom"]
    assert "сломалась" in plugins.status(db, "boom")["error"]
    plugins.registry().set_enabled(db, "boom", False)
    g = gate(db, sig, {}, plugins.user_settings(db))
    assert not g["ok"] and "выключены" in g["verdicts"][""]


@pytest.fixture(scope="module")
def world_db():
    from whalefollow.positions import assign_entities, build_positions
    from whalefollow.scoring.scorer import run_scoring
    from whalefollow.scoring.sybil import run_antisybil

    w = SyntheticWorld(seed=4)
    d = DB("sqlite://:memory:")
    d.init_schema()
    load_world(d, w)
    p = fast_params()
    build_positions(d)
    run_antisybil(d, p)
    assign_entities(d)
    sc = run_scoring(d, p, now=w.now)
    return {"db": d, "p": p, "now": w.now, "best": approved_ids(sc), "world": w}


def test_backtest_copy_whale_matches_whale_sim(world_db):
    d, p, now, best = world_db["db"], world_db["p"], world_db["now"], world_db["best"]
    d.execute("DELETE FROM watchlist")
    _watch(d, best[0])
    ws = [r["wallet"] for r in d.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (best[0],))] or [best[0]]
    settings = plugins.user_settings(d)
    one = simulate_whale(d, p, ws, settings, now, days=180)["summary"]
    bt = backtest(d, p, "copy_whale", days=180, now=now, settings=settings)
    assert bt["ok"] and bt["summary"]["n"] == one["n"] > 0
    assert bt["summary"]["closed_pnl_usd"] == pytest.approx(one["closed_pnl_usd"])


def test_backtest_strategies_differ_and_never_exceed_copy_all(world_db):
    d, p, now, best = world_db["db"], world_db["p"], world_db["now"], world_db["best"]
    d.execute("DELETE FROM watchlist")
    for e in best[:4]:
        _watch(d, e)
    settings = plugins.user_settings(d)
    allb = backtest(d, p, "copy_whale", days=365, now=now, settings=settings)["summary"]
    cons = backtest(d, p, "consensus", days=365, now=now, settings=settings)["summary"]
    assert allb["n"] > 0 and cons["n"] <= allb["n"]
    assert cons["skipped"].get("strategy", 0) > 0  # консенсус отбрасывает одиночные покупки

    class Never(StrategyPlugin):
        name = "never"

        def accept(self, c):
            return Verdict(False, "нет")

    plugins.registry().register(Never(), "тест")
    nv = backtest(d, p, "never", days=365, now=now, settings=settings)["summary"]
    assert nv["n"] == 0 and nv["skipped"]["strategy"] > 0 and "ни одной" in nv["text"]
    assert backtest(d, p, "nope")["ok"] is False


# ---------------------------------------------------------------- API экрана «Плагины»
def test_plugins_api(world_db):
    from whalefollow.api.server import serve
    from whalefollow.services import Services

    d, p, now = world_db["db"], world_db["p"], world_db["now"]
    svc = Services(d, p, clock=lambda: now)
    httpd = serve(svc, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    try:
        lst = call("GET", "/plugins")
        by = {i["name"]: i for i in lst["items"]}
        assert by["copy_whale"]["state"] in ("idle", "ok") and by["copy_whale"]["enabled"]
        assert by["bookmakers"]["state"] in ("unavailable", "off")
        assert [k["kind"] for k in lst["kinds"]] == ["source", "estimator", "notifier", "strategy"]
        r = call("PATCH", "/plugins/copy_whale", {"enabled": False})
        assert r["ok"] and r["warning"] == "Все стратегии выключены — новых сигналов не будет"
        assert call("PATCH", "/plugins/copy_whale", {"enabled": True})["warning"] is None
        assert not call("PATCH", "/plugins/nope", {"enabled": True})["ok"]
        bt = call("GET", "/plugins/copy_whale/backtest?days=365")
        assert bt["ok"] and "summary" in bt
    finally:
        httpd.shutdown()


def test_agent_skips_signal_when_no_strategy_takes_it(world_db):
    """Живой путь: стратегия-плагин из файла решает, будет ли сигнал; причина видна в журнале."""
    from whalefollow.agent import Agent
    from whalefollow.estimator.base import FunctionEstimator
    from whalefollow.ingest.polymarket import parse_market

    from synthetic import FakeClob, FakeData, FakeGamma

    d, p, w, best = world_db["db"], world_db["p"], world_db["world"], world_db["best"]
    d.execute("DELETE FROM watchlist")
    whale = None
    for e in best:
        cats = [
            r["category"]
            for r in d.query(
                "SELECT category FROM wallet_category_scores WHERE entity_id=? AND status='approved'", (e,)
            )
            if r["category"] in ("economics", "politics", "sports", "tech")
        ]
        if cats:
            whale, cat = e, cats[0]
            break
    assert whale
    _watch(d, whale)
    agent = Agent(
        d, p, FakeData(w), FakeClob(w), FakeGamma(w), estimator=FunctionEstimator(lambda c: 0.72), clock=lambda: w.now
    )
    agent.run_once()  # догрузка истории

    class BigOnly(StrategyPlugin):
        name = "big_only"
        default_enabled = True

        def accept(self, c):
            return Verdict(c.size_usd >= 5000, "крупная" if c.size_usd >= 5000 else "мелкая")

    reg = plugins.registry()
    reg.register(BigOnly(), "файл big.py")
    reg.set_enabled(d, "copy_whale", False)

    def trade(usd):
        m = w.add_live_market(category=cat, pi=0.75, price=0.55)
        mrow, toks, _ = parse_market(w.gamma_market(m))
        d.upsert("markets", mrow, ["market_id"])
        d.upsert_many("tokens", toks, ["token_id"])
        w.whale_trade_now(agent.wallets_of(whale)[0], m, m.yes_token, usd)
        agent.run_once()
        return d.one(
            "SELECT s.status, d.reasons FROM signals s JOIN decisions d ON d.signal_id=s.signal_id "
            "WHERE s.market_id=? ORDER BY s.ts DESC LIMIT 1",
            (m.market_id,),
        )

    small = trade(3000)
    assert small["status"] == "skipped" and json.loads(small["reasons"])["codes"] == ["strategy"]
    assert json.loads(small["reasons"])["details"]["strategies"] == {"big_only": "мелкая"}
    big = trade(6000)
    assert big["status"] == "pending"
    assert json.loads(big["reasons"])["details"]["strategies"] == ["big_only"]
