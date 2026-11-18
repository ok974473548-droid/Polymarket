"""Блок 2: пароль, защита от подделки запросов, параллельные запросы к API, Telegram-кнопки, копии, сбои."""

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from whalefollow.api.server import serve
from whalefollow.auth import Auth
from whalefollow.cli import resolve_db_url
from whalefollow.db import DB
from whalefollow.notifier import TelegramNotifier
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services

from conftest import fast_params


def _req(port, path, method="GET", body=None, cookie=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
    h = {}
    if method != "GET" and ctype:
        h["Content-Type"] = ctype
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


@pytest.fixture()
def server(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/a.db")
    db.init_schema()
    svc = Services(db, fast_params())
    auth = Auth(db, password="s3cret")
    httpd = serve(svc, port=0, auth=auth)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], svc, auth
    httpd.shutdown()


def test_password_login_flow(server):
    port, _, _ = server
    assert _req(port, "/api/status")[0] == 401  # без входа данных нет
    assert _req(port, "/api/auth")[1] == {"required": True, "ok": False}
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
        assert r.status == 200  # страница входа отдаётся
    assert _req(port, "/api/login", "POST", {"password": "wrong"})[0] == 401
    code, body, headers = _req(port, "/api/login", "POST", {"password": "s3cret"})
    assert code == 200 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";")[0]
    assert _req(port, "/api/status", cookie=cookie)[0] == 200
    assert _req(port, "/api/auth", cookie=cookie)[1]["ok"] is True
    assert _req(port, "/api/status", cookie="wf_session=999.forged")[0] == 401


def test_bruteforce_lockout():
    db = DB("sqlite://:memory:")
    db.init_schema()
    a = Auth(db, password="pw")
    for _ in range(5):
        assert a.login("x", "1.2.3.4", now=1000) is None
    assert a.blocked("1.2.3.4", now=1001) and a.login("pw", "1.2.3.4", now=1002) is None
    assert a.login("pw", "1.2.3.4", now=1000 + 601) is not None  # через 10 минут снова можно
    assert a.login("pw", "5.6.7.8", now=1000) is not None  # другой адрес не затронут


def test_password_change_invalidates_sessions():
    db = DB("sqlite://:memory:")
    db.init_schema()
    tok = Auth(db, password="old").token()
    assert Auth(db, password="old").valid(tok) and not Auth(db, password="new").valid(tok)


def test_csrf_needs_json_and_no_cors(server):
    port, _, auth = server
    cookie = "wf_session=" + auth.token()
    code, _, headers = _req(port, "/api/stop", "POST", cookie=cookie, ctype="text/plain")
    assert code == 415  # «простой» запрос с чужого сайта отклонён
    code, _, headers = _req(port, "/api/status", cookie=cookie)
    assert "Access-Control-Allow-Origin" not in headers and headers["X-Frame-Options"] == "DENY"


def test_api_requests_run_in_parallel(server):
    port, svc, auth = server
    cookie = "wf_session=" + auth.token()
    orig = svc.status

    def slow_status():
        time.sleep(0.6)
        return orig()

    svc.status = slow_status
    t0 = time.time()
    th = [threading.Thread(target=_req, args=(port, "/api/status"), kwargs={"cookie": cookie}) for _ in range(4)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert time.time() - t0 < 1.5  # раньше шли по очереди: 4 × 0,6 с


class _TgSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], json))

        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": []}

        return R()


class _Agent:
    def __init__(self):
        self.done = []

    def confirm(self, sid):
        self.done.append(("confirm", sid))
        return {"status": "executed", "shares": 80.0, "avg_price": 0.5}

    def reject(self, sid):
        self.done.append(("reject", sid))
        return {"status": "rejected"}

    def ai_mode(self, settings=None):
        return {"selected": "manual", "available": False, "effective": "manual"}

    def resolve_exit(self, sid, action):
        self.done.append((action, sid))
        return {"status": "sold" if action == "sell" else "held"}


def test_telegram_buttons_and_commands():
    db = DB("sqlite://:memory:")
    db.init_schema()
    sess = _TgSession()
    tg = TelegramNotifier(db, "T", "42", session=sess)
    svc = Services(db, fast_params())
    svc.agent = _Agent()
    tg.attach(svc, start_polling=False)
    db.listeners.append(tg.on_notify)
    db.notify("signal", "Новый сигнал", "Вопрос?", "s_abc")
    tg.q.join()
    method, payload = sess.calls[-1]
    assert method == "sendMessage"
    kb = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["c:s_abc", "r:s_abc"]
    # нажатие из нашего чата — покупка
    text = tg.handle_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "q1",
                "data": "c:s_abc",
                "message": {"message_id": 7, "chat": {"id": 42}, "text": "x"},
            },
        }
    )
    assert "Куплено" in text and svc.agent.done == [("confirm", "s_abc")]
    assert {"answerCallbackQuery", "editMessageText"} <= {c[0] for c in sess.calls}
    # нажатие из чужого чата игнорируется
    assert (
        tg.handle_update(
            {"update_id": 2, "callback_query": {"id": "q2", "data": "c:s_x", "message": {"chat": {"id": 666}}}}
        )
        is None
    )
    assert len(svc.agent.done) == 1
    assert "Остановлено" in tg.handle_update({"update_id": 3, "message": {"chat": {"id": 42}, "text": "/stop"}})
    assert svc.risk.stopped()
    assert (
        tg.handle_update({"update_id": 4, "message": {"chat": {"id": 42}, "text": "/resume"}}) == "Работа продолжена."
    )
    assert not svc.risk.stopped()
    assert tg.handle_update({"update_id": 5, "message": {"chat": {"id": 7}, "text": "/stop"}}) is None


def test_backup_and_failure_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("WF_BACKUP_DIR", str(tmp_path / "bk"))
    db = DB(f"sqlite:///{tmp_path}/main.db")
    db.init_schema()
    db.set_setting("x", 1)
    clock = {"t": 1_790_000_000}
    sch = Scheduler(db, fast_params(), None, None, None, agent=None, clock=lambda: clock["t"])
    path = sch.job_backup()
    assert os.path.exists(path)
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] >= 1
    c.close()
    for _d in range(10):  # хранится 7 последних
        clock["t"] += 86400
        sch.job_backup()
    assert len(os.listdir(tmp_path / "bk")) == 7
    # три сбоя подряд → одно уведомление; восстановление → ещё одно
    for j in sch.jobs:
        j.fn = lambda: None  # в тесте нет клиентов API
    job = next(j for j in sch.jobs if j.name == "markets")
    job.fn = lambda: (_ for _ in ()).throw(RuntimeError("нет сети"))
    for _ in range(4):
        clock["t"] += 10_000
        sch.run_due("slow")
    alerts = db.query("SELECT title FROM notifications WHERE kind='system'")
    assert [a["title"] for a in alerts] == ["Не работает: Каталог рынков"]
    job.fn = lambda: 1
    clock["t"] += 10_000
    sch.run_due("slow")
    assert db.scalar("SELECT COUNT(*) AS n FROM notifications WHERE title='Снова работает: Каталог рынков'") == 1


def test_docker_db_path_moved_to_volume():
    assert resolve_db_url("sqlite:///whalefollow.db", in_docker=True) == "sqlite:////data/whalefollow.db"
    assert resolve_db_url("sqlite:////data/a.db", in_docker=True) == "sqlite:////data/a.db"
    assert resolve_db_url("sqlite:///whalefollow.db", in_docker=False) == "sqlite:///whalefollow.db"
