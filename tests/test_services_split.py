"""Этап 2 «Разделение на сервисы»: три процесса (api / realtime / worker) общаются только через базу.

Каждый «процесс» в тестах — отдельный объект DB с собственными соединениями к одному файлу базы:
так же, как три контейнера с общим томом /data.
"""

import time

import pytest

from whalefollow import cli
from whalefollow.db import DB
from whalefollow.positions import assign_entities, build_positions
from whalefollow.runtime import CheckRunner, Heartbeat, QuotePublisher, services_state
from whalefollow.scheduler import Scheduler, scheduler_state
from whalefollow.scoring.scorer import run_scoring
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world


def wait_for(cond, timeout=15.0, step=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def _proc_db(path):
    """База «другого процесса»: тот же файл, свои соединения."""
    d = DB(f"sqlite:///{path}")
    d.init_schema()
    return d


# ---------------------------------------------------------------- живые события между процессами
def test_signal_from_realtime_reaches_api_page(tmp_path):
    from test_live_ui import _pending_signal

    path = tmp_path / "wf.db"
    api_db, rt_db = _proc_db(path), _proc_db(path)
    gen = Services(api_db, fast_params()).event_stream(tick=0.05, max_s=10)
    assert next(gen)[0] == "hello"
    _pending_signal(rt_db, "s_cross")  # сигнал записан процессом realtime
    rt_db.publish("signal", signal_id="s_cross", status="pending")
    t0 = time.time()
    for kind, data in gen:
        if kind == "change":
            assert "s_cross" in data["new_pending"]
            break
    assert time.time() - t0 < 2  # страница узнаёт за доли секунды (раньше — только в одном процессе)
    assert api_db.scalar("SELECT COUNT(*) AS n FROM live_events WHERE kind='signal'") == 1


def test_live_events_are_pruned(tmp_path):
    db = _proc_db(tmp_path / "e.db")
    db.execute("INSERT INTO live_events (ts, kind, data) VALUES (?, 'old', '{}')", (time.time() - 7200,))
    db._published = 499
    db.publish("x")
    assert db.scalar("SELECT COUNT(*) AS n FROM live_events WHERE kind='old'") == 0


# ---------------------------------------------------------------- проверка игрока: api ставит, worker выполняет
def test_player_check_runs_in_worker(tmp_path):
    world = SyntheticWorld(seed=5, n_events=120, n_random=10)
    path = tmp_path / "wf.db"
    api_db, worker_db = _proc_db(path), _proc_db(path)
    p = fast_params()
    api = Services(api_db, p, data=FakeData(world), gamma=FakeGamma(world), clock=lambda: world.now)
    api.check_mode = "queue"
    worker = Services(worker_db, p, data=FakeData(world), gamma=FakeGamma(world), clock=lambda: world.now)
    wallet = next(w for w, r in world.roles.items() if r == "skilled")

    first = api.start_check(wallet, wait_s=0)
    assert first["ok"] and first["state"] == "running" and first["queued"]  # для экрана — «идёт», в очереди
    assert api.start_check(wallet, wait_s=0)["job_id"] == first["job_id"]  # повтор не ставит вторую проверку
    assert api._check_threads == {}  # сам api ничего не выполняет

    runner = CheckRunner(worker, poll_s=0.05).start()
    try:
        assert wait_for(lambda: api.check_status(first["job_id"])["state"] == "done", timeout=60)
    finally:
        runner.stop()
    st = api.check_status(first["job_id"])
    assert st["checks"] and st["stats"]["trades"] > 0 and not st["queued"]
    assert all(s["status"] == "done" for s in st["stages"]) and runner.ran == 1
    assert api_db.one("SELECT worker FROM check_jobs WHERE job_id=?", (first["job_id"],))["worker"] == "worker"


def test_stale_check_returns_to_queue(tmp_path):
    db = _proc_db(tmp_path / "c.db")
    svc = Services(db, fast_params())
    job = {
        "job_id": "chk_1",
        "address": "0x" + "1" * 40,
        "state": "running",
        "stage": "trades",
        "done": 5,
        "total": 0,
        "started": time.time() - 900,
        "finished": 0.0,
        "stages_done": [],
        "worker": "worker",
    }
    svc._save_job(job, force=True)
    db.execute("UPDATE check_jobs SET updated_ts=? WHERE job_id='chk_1'", (time.time() - 600,))
    r = CheckRunner(svc)
    assert r.requeue_stale() == 1  # worker упал посреди проверки — вернулась в очередь
    assert db.one("SELECT state FROM check_jobs WHERE job_id='chk_1'")["state"] == "queued"
    r.max_parallel = 0
    assert r.claim() == []  # без свободных мест ничего не берёт


# ---------------------------------------------------------------- пульс процессов и «Диагностика»
def test_heartbeats_and_diagnostics(tmp_path):
    db = _proc_db(tmp_path / "h.db")
    for role in ("api", "realtime", "worker"):
        Heartbeat(db, role, info_fn=lambda: {"jobs": ["slow"]}).beat()
    db.upsert(
        "service_status",
        {"name": "all", "pid": 1, "host": "h", "version": "0.14.0", "started_ts": 1.0, "last_ts": 2.0, "info": "{}"},
        ["name"],
    )
    db.execute("UPDATE service_status SET last_ts=? WHERE name='worker'", (time.time() - 120,))
    st = {s["name"]: s for s in services_state(db)}
    assert set(st) == {"api", "realtime", "worker"}  # старая запись «всё в одном» скрыта
    assert st["api"]["alive"] and st["realtime"]["alive"] and not st["worker"]["alive"]
    assert st["worker"]["last_seen_s"] >= 119
    d = Services(db, fast_params()).diagnostics(live=False)
    assert {s["name"] for s in d["services"]} == {"api", "realtime", "worker"}
    assert "ERR процесс Фоновые задачи: нет пульса" in d["report"]
    assert cli.main(["--db", f"sqlite:///{tmp_path / 'h.db'}", "alive", "realtime"]) == 0
    assert cli.main(["--db", f"sqlite:///{tmp_path / 'h.db'}", "alive", "worker"]) == 1


def test_streams_state_comes_from_realtime_heartbeat(tmp_path):
    db = _proc_db(tmp_path / "s.db")
    streams = {
        "trades": {"started": True, "live": True},
        "books": {"started": True, "live": True},
        "agent_books": {"from_stream": 3, "by_request": 0, "request_median_ms": None},
    }
    Heartbeat(db, "realtime", info_fn=lambda: {"streams": streams}).beat()
    assert Services(db, fast_params()).streams_status() == streams  # api без своих потоков видит их состояние


# ---------------------------------------------------------------- живые цены: realtime → api
class _Client:
    generation = 1

    def alive(self, s):
        return True


class _Books:
    STALE_S = 25

    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self._client = _Client()
        self.books = {"tok1": {"bids": {0.51: 10, 0.5: 5}, "asks": {0.53: 7, 0.6: 1}, "gen": 1, "ts": time.time()}}


def test_live_quotes_from_realtime(tmp_path):
    path = tmp_path / "q.db"
    rt_db, api_db = _proc_db(path), _proc_db(path)
    pub = QuotePublisher(rt_db, _Books())
    assert pub.publish_once() == 1 and pub.publish_once() == 0  # без изменений — не пишет
    q = Services(api_db, fast_params()).quote("tok1")
    assert q == {"bid": 0.51, "ask": 0.53, "live": True}
    api_db.execute("UPDATE live_quotes SET ts=?", (time.time() - 120,))
    assert Services(api_db, fast_params()).quote("tok1") is None  # устаревшая цена «живой» не считается


# ---------------------------------------------------------------- планировщик по ролям
def test_scheduler_groups_and_merged_state(db):
    world = SyntheticWorld(seed=1, n_events=10, n_random=2)
    rt = Scheduler(
        db,
        fast_params(),
        FakeGamma(world),
        FakeData(world),
        FakeClob(world),
        None,
        groups=("fast", "books"),
        role="realtime",
    )
    assert rt.state_key == "scheduler:realtime"
    db.set_state("scheduler", {"agent": {"last_run": 100, "ok": True}, "score": {"last_run": 50, "ok": True}})
    db.set_state("scheduler:realtime", {"agent": {"last_run": 200, "ok": False}})
    db.set_state("scheduler:worker", {"score": {"last_run": 300, "ok": True}})
    st = scheduler_state(db)
    assert st["agent"]["last_run"] == 200 and st["agent"]["ok"] is False and st["score"]["last_run"] == 300


# ---------------------------------------------------------------- сигналы не зависят от worker
@pytest.fixture()
def world_db(tmp_path):
    world = SyntheticWorld(seed=4)
    path = tmp_path / "world.db"
    db = _proc_db(path)
    load_world(db, world)
    params = fast_params()
    build_positions(db)
    run_antisybil(db, params)
    assign_entities(db)
    sc = run_scoring(db, params, now=world.now)
    whale = cat = None
    for e in sc[sc.status == "approved"].sort_values("score", ascending=False)["entity_id"]:
        cats = [
            r["category"]
            for r in db.query(
                "SELECT category FROM wallet_category_scores WHERE entity_id=? AND status='approved'", (e,)
            )
            if r["category"] in ("economics", "politics", "sports", "tech")
        ]
        if cats:
            whale, cat = e, cats[0]
            break
    Services(db, params, clock=lambda: world.now).set_whale(whale, mode="copy")
    return world, path, whale, cat


def test_realtime_makes_signals_without_worker(world_db, monkeypatch):
    from whalefollow.ingest.polymarket import parse_market

    world, path, whale, cat = world_db
    monkeypatch.setenv("WF_STREAMS", "off")
    for k in ("WF_TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rt_db = _proc_db(path)
    svc, sched, _ = cli.run_role(
        "realtime",
        rt_db,
        fast_params(),
        FakeGamma(world),
        FakeData(world),
        FakeClob(world),
        block=False,
        clock=lambda: world.now,
    )
    try:
        assert sched.groups == ("fast", "books")
        assert wait_for(lambda: (rt_db.get_state("scheduler:realtime") or {}).get("agent", {}).get("runs"), 30)
        m = world.add_live_market(category=cat, pi=0.75, price=0.55)  # новая сделка кита после старта
        mrow, toks, _ = parse_market(world.gamma_market(m))
        rt_db.upsert("markets", mrow, ["market_id"])
        rt_db.upsert_many("tokens", toks, ["token_id"])
        world.whale_trade_now(svc.agent.wallets_of(whale)[0], m, m.yes_token, 3000)
        assert wait_for(lambda: rt_db.scalar("SELECT COUNT(*) AS n FROM signals", default=0) > 0, timeout=30)
        st = {s["name"]: s for s in services_state(rt_db)}
        assert st["realtime"]["alive"] and "worker" not in st  # worker не запущен вовсе — сигналы есть
        assert svc.status()["agent_ok"]
        assert "score" not in (rt_db.get_state("scheduler:realtime") or {})  # тяжёлое — не здесь
    finally:
        sched.stop()
