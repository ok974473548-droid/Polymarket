"""Этап 1 «Фундамент»: миграции Alembic, сеть (httpx), сервер FastAPI, журнал structlog, WebSocket."""

import asyncio
import gzip
import http.client
import io
import json
import logging
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request

import httpx
import pytest

from whalefollow import migrations, net
from whalefollow.api.server import serve
from whalefollow.auth import Auth
from whalefollow.db import DB
from whalefollow.services import Services
from whalefollow.util import RateLimiter
from whalefollow.ws import _proxy_for

from conftest import fast_params


# ---------------------------------------------------------------- миграции
def _legacy_db(path):
    """База «как до Alembic»: схема 0.7 без поздних колонок, с данными, без отметки версии."""
    from importlib import resources

    sql = resources.files("whalefollow").joinpath("schema.sql").read_text(encoding="utf-8")
    c = sqlite3.connect(path)
    for st in sql.split(";"):
        body = "\n".join(ln for ln in st.splitlines() if not ln.strip().startswith("--")).strip()
        if body and "ix_trades_source" not in body:
            c.execute(body)
    c.execute("INSERT INTO markets (market_id, question, status) VALUES ('m1', 'Will it rain?', 'open')")
    c.execute(
        "INSERT INTO trades (trade_id, ts, ts_fetched, wallet, token_id, side, price, size_shares, size_usd) "
        "VALUES ('t1', 1, 1, '0xw', 'tok', 'BUY', 0.5, 10, 5)"
    )
    c.execute("INSERT INTO settings (key, value) VALUES ('bankroll_usd', '1234')")
    c.commit()
    c.close()


def test_legacy_database_is_stamped_and_keeps_data(tmp_path):
    p = tmp_path / "old.db"
    _legacy_db(p)
    db = DB(f"sqlite:///{p}")
    assert db.schema_version() is None
    assert db.init_schema() == migrations.head() == "0001"
    assert db.schema_version() == "0001"
    assert {"fee_rate", "url"} <= db._columns("markets") and "source" in db._columns("trades")
    assert {"detected_ts", "info"} <= db._columns("signals")
    assert db.one("SELECT question FROM markets WHERE market_id='m1'")["question"] == "Will it rain?"
    assert db.scalar("SELECT COUNT(*) AS n FROM trades") == 1 and db.get_setting("bankroll_usd") == 1234
    assert db.one("SELECT name FROM sqlite_master WHERE name='ix_trades_source'")
    assert db.init_schema() == "0001"  # повторный запуск — ничего не меняет


def test_new_database_is_on_head(db):
    assert db.schema_version() == migrations.head()
    assert db.scalar("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'") >= 30


def test_next_migration_is_applied(tmp_path):
    """Следующие изменения схемы приходят миграциями: новая версия применяется к базе на 0001."""
    loc = tmp_path / "mig"
    shutil.copytree(migrations.HERE, loc, ignore=shutil.ignore_patterns("__pycache__", "__init__.py"))
    (loc / "versions" / "0002_demo.py").write_text(
        'revision = "0002"\ndown_revision = "0001"\nbranch_labels = None\ndepends_on = None\n'
        "from alembic import op\nimport sqlalchemy as sa\n\n"
        "def upgrade():\n    with op.batch_alter_table('markets') as b:\n"
        "        b.add_column(sa.Column('demo_note', sa.Text()))\n\n"
        "def downgrade():\n    with op.batch_alter_table('markets') as b:\n        b.drop_column('demo_note')\n",
        encoding="utf-8",
    )
    p = tmp_path / "a.db"
    db = DB(f"sqlite:///{p}")
    db.init_schema()
    db.execute("INSERT INTO markets (market_id, question) VALUES ('m1', 'q')")
    assert migrations.upgrade(db, location=str(loc)) == "0002"
    assert "demo_note" in db._columns("markets") and db.scalar("SELECT COUNT(*) AS n FROM markets") == 1


# ---------------------------------------------------------------- сеть
class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = code, body, headers or {}, ""

    def json(self):
        return self._body


class _Seq:
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.resps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_retry_after_header_is_respected():
    waits = []
    s = _Seq(_Resp(429, headers={"Retry-After": "7"}), _Resp(503), httpx.ConnectError("reset"), _Resp(200, {"ok": 1}))
    assert net.get_json(s, "https://x.test/a", retries=4, sleep=waits.append) == {"ok": 1}
    assert waits == [7.0, 2.0, 4.0]  # Retry-After, затем 2^n
    assert (
        net.retry_after_s(_Resp(429, headers={"Retry-After": "Wed, 18 Nov 2026 00:00:30 GMT"}), now=1794960000) == 30.0
    )
    assert net.retry_after_s(_Resp(429, headers={"Retry-After": "9999"})) == net.MAX_RETRY_AFTER_S
    assert net.retry_after_s(_Resp(429)) is None


def test_client_errors_are_not_retried():
    s = _Seq(_Resp(410, {"error": "gone"}), _Resp(200, {}))
    with pytest.raises(net.ApiError, match="HTTP 410") as e:
        net.get_json(s, "https://x.test/v1", sleep=lambda d: None)
    assert s.calls == 1 and e.value.status == 410
    s = _Seq(*[_Resp(500)] * 3)
    with pytest.raises(net.ApiError, match="HTTP 500"):
        net.get_json(s, "https://x.test/b", retries=3, sleep=lambda d: None)
    assert s.calls == 3


def test_async_get_with_mock_transport():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"n": len(calls)})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await net.aget_json(c, "https://x.test/q", params={"a": 1}, before=RateLimiter(0).await_)

    assert asyncio.run(go()) == {"n": 2} and calls[0].endswith("/q?a=1")


def test_shared_client_settings():
    c = net.client()
    assert c is net.client() and c.headers["User-Agent"].startswith("whalefollow/") and c.follow_redirects
    assert c.timeout.connect == 10.0 and c.timeout.read == 15.0


# ---------------------------------------------------------------- сервер
@pytest.fixture()
def api(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/a.db")
    db.init_schema()
    httpd = serve(Services(db, fast_params()), port=0, auth=Auth(db, password=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _call(port, method, path, body=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers={"Content-Type": ctype} if body is not None else {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def test_openapi_docs_and_models(api):
    code, body, _ = _call(api, "GET", "/openapi.json")
    spec = json.loads(body)
    assert code == 200 and spec["info"]["title"] == "WhaleFollow API"
    assert {"/api/status", "/api/dashboard", "/api/whales/check", "/api/players/add", "/api/events"} <= set(
        spec["paths"]
    )
    assert "AddPlayersIn" in spec["components"]["schemas"] and "StatusOut" in spec["components"]["schemas"]
    code, body, _ = _call(api, "GET", "/docs")
    assert code == 200 and b"swagger" in body.lower()


def test_api_errors_are_json_with_old_codes(api):
    assert _call(api, "POST", "/api/whales", "{bad")[0] == 400  # неверный JSON
    code, body, _ = _call(api, "POST", "/api/whales", "{}")
    assert code == 400 and json.loads(body)["error"] == "Нужен entity_id"
    code, body, _ = _call(api, "PATCH", "/api/whales/x", '{"enabled": "maybe"}')
    assert code == 400 and "enabled" in json.loads(body)["error"]  # проверка pydantic
    code, body, _ = _call(api, "GET", "/api/stop")  # есть только POST
    assert code == 404 and "Нет маршрута" in json.loads(body)["error"]
    code, _, r = _call(api, "HEAD", "/")
    assert code == 200 and r.getheader("ETag")
    code, _, r = _call(api, "GET", "/api/status")
    assert r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Referrer-Policy") == "no-referrer"
    assert r.getheader("Server") is None and r.getheader("Access-Control-Allow-Origin") is None


def test_large_json_is_gzipped(api):
    req = urllib.request.Request(f"http://127.0.0.1:{api}/openapi.json", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    assert json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)["openapi"]
    from starlette.requests import Request

    from whalefollow.api.routes import json_response

    req = Request({"type": "http", "headers": [(b"accept-encoding", b"gzip, br")]})
    big = {"items": [{"name": f"кит {i}", "pnl": i * 1.5} for i in range(200)]}
    r = json_response(req, big)
    assert r.headers["content-encoding"] == "gzip" and json.loads(gzip.decompress(r.body)) == big
    small = json_response(req, {"ok": True})
    assert "content-encoding" not in small.headers and small.headers["cache-control"] == "no-store"


def test_cors_only_when_enabled(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/c.db")
    db.init_schema()
    httpd = serve(Services(db, fast_params()), port=0, auth=Auth(db, password=""), cors_origin="https://my.app")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/status", timeout=10) as r:
            assert r.headers["Access-Control-Allow-Origin"] == "https://my.app"
    finally:
        httpd.shutdown()


def test_unauthorized_post_with_body_keeps_connection(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/p.db")
    db.init_schema()
    httpd = serve(Services(db, fast_params()), port=0, auth=Auth(db, password="pw"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("POST", "/api/stop", body=json.dumps({"x": "y" * 5000}), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 401 and json.loads(r.read())["auth"] is True
        c.request("GET", "/api/auth")
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"required": True, "ok": False}
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------- журнал и WebSocket
def test_structlog_json_and_console():
    from whalefollow.logs import get_logger, setup_logging

    buf = io.StringIO()
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        setup_logging(logging.INFO, fmt="json", stream=buf)
        logging.getLogger("whalefollow.test").info("старое сообщение %s", 42)
        get_logger("whalefollow.test").info("новое", wallet_count=3)
        lines = [json.loads(x) for x in buf.getvalue().splitlines()]
        assert lines[0]["event"] == "старое сообщение 42" and lines[0]["level"] == "info"
        assert (
            lines[1]["event"] == "новое" and lines[1]["wallet_count"] == 3 and lines[1]["logger"] == "whalefollow.test"
        )
        buf2 = io.StringIO()
        setup_logging(logging.INFO, fmt="console", stream=buf2)
        logging.getLogger("whalefollow.test").warning("в консоли")
        assert "в консоли" in buf2.getvalue() and not buf2.getvalue().startswith("{")
    finally:
        for h in list(root.handlers):
            if getattr(h, "_whalefollow", False):
                root.removeHandler(h)
        for h in handlers:
            if h not in root.handlers:
                root.addHandler(h)
        root.setLevel(level)


def test_ws_proxy_only_for_wss_and_respects_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,.internal")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    assert _proxy_for("ws-live-data.polymarket.com", "wss") == "http://proxy.local:3128"
    assert _proxy_for("ws-live-data.polymarket.com", "ws") is None
    assert _proxy_for("feed.internal", "wss") is None and _proxy_for("localhost", "wss") is None
