"""Performance benchmark of Kontur: network, local server, database, copy detection.

Run from the repository root (needs internet for the network part):
    PYTHONPATH=. python research/bench_perf.py [--no-net]
Prints one JSON object; run it before and after a change and compare.
"""

import gzip
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from kontur import db as store
from kontur import polymarket as pm

OUT = {}


def ms(seconds):
    return round(seconds * 1000, 1)


# ---------------------------------------------------------------- network
def live_tokens(n):
    rows = pm.fetch(pm.GAMMA + "/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false")
    toks = []
    for m in rows:
        if m.get("enableOrderBook") and m.get("acceptingOrders") and m.get("clobTokenIds"):
            toks.append(json.loads(m["clobTokenIds"])[0])
    good = []
    for t in toks:
        try:
            pm.book(t)
            good.append(t)
        except (pm.UpstreamError, LookupError):
            pass
        if len(good) >= n:
            break
    return good


def bench_network():
    toks = live_tokens(40)
    res = {}
    t = time.perf_counter()
    for tok in toks[:20]:
        pm.book(tok)
    res["book_sequential_ms"] = ms((time.perf_counter() - t) / 20)
    from kontur import worker
    t = time.perf_counter()
    worker._books(toks)
    res["books_40_total_ms"] = ms(time.perf_counter() - t)
    t = time.perf_counter()
    for _ in range(5):
        pm.trades_page(limit=500, taker_only=False)
    res["data_trades_500_ms"] = ms((time.perf_counter() - t) / 5)
    t = time.perf_counter()
    for _ in range(5):
        pm.fetch(pm.GAMMA + "/events?limit=50&active=true&closed=false")
    res["gamma_events_50_ms"] = ms((time.perf_counter() - t) / 5)
    rows = pm.trades_page(limit=100, taker_only=False)["rows"]
    now = time.time()
    res["data_api_newest_trade_age_s"] = round(min(now - int(r["timestamp"] or 0) for r in rows), 1)
    return res


def bench_stream(seconds=15):
    try:
        from kontur.ws import WebSocket
    except ImportError:
        return {"rtds": "нет клиента WebSocket"}
    ws = WebSocket("wss://ws-live-data.polymarket.com")
    ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}))
    ws.settimeout(10)
    lags, t0 = [], time.time()
    while time.time() - t0 < seconds:
        m = ws.recv()
        if m.startswith("{"):
            p = (json.loads(m).get("payload") or {})
            if p.get("timestamp"):
                ts = p["timestamp"] / 1000 if p["timestamp"] > 1e12 else p["timestamp"]
                lags.append(time.time() - ts)
    ws.close()
    lags.sort()
    return {"rtds_trades_per_s": round(len(lags) / seconds, 1), "rtds_lag_median_s": round(lags[len(lags) // 2], 2),
            "rtds_lag_p90_s": round(lags[int(len(lags) * 0.9)], 2)}


# ---------------------------------------------------------------- local server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def bench_server():
    tmp = tempfile.mkdtemp()
    port = free_port()
    proc = subprocess.Popen([sys.executable, "app.py", "--no-worker", "--port", str(port), "--db", os.path.join(tmp, "b.sqlite3")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/api/health")
                c.getresponse().read()
                break
            except OSError:
                time.sleep(0.1)
        res = {}

        def run(path, n=200, headers=None, keep=True):
            times, size, enc = [], 0, None
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            for _ in range(n):
                t = time.perf_counter()
                try:
                    c.request("GET", path, headers=headers or {})
                    r = c.getresponse()
                    body = r.read()
                except (http.client.HTTPException, OSError):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    c.request("GET", path, headers=headers or {})
                    r = c.getresponse()
                    body = r.read()
                times.append(time.perf_counter() - t)
                size, enc = len(body), r.getheader("Content-Encoding")
                if r.getheader("Connection", "").lower() == "close" or r.version == 10 or not keep:
                    c.close()
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            return {"avg_ms": ms(statistics.mean(times)), "p90_ms": ms(sorted(times)[int(n * 0.9)]), "bytes": size, "enc": enc}

        res["api_health"] = run("/api/health")
        res["page_index"] = run("/", headers={"Accept-Encoding": "gzip"})
        res["static_app_js"] = run("/static/js/app.js?v=1", headers={"Accept-Encoding": "gzip"})
        res["static_core_js_bytes_gzip"] = run("/static/js/core.js", n=5, headers={"Accept-Encoding": "gzip"})["bytes"]
        # repeat view of a static file: does the browser get 304?
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/static/js/core.js")
        r = c.getresponse()
        r.read()
        etag = r.getheader("ETag")
        if etag:
            c.request("GET", "/static/js/core.js", headers={"If-None-Match": etag})
            r2 = c.getresponse()
            r2.read()
            res["static_revalidate_status"] = r2.status
        else:
            res["static_revalidate_status"] = "нет ETag (файл каждый раз целиком)"
        # all static files of a first visit (what the browser downloads)
        total = 0
        for path in ["/", "/static/css/app.css", "/static/vendor/lightweight-charts.standalone.production.js", "/static/js/app.js",
                     "/static/js/core.js", "/static/js/layout.js", "/static/js/cards.js", "/static/js/chart.js"]:
            c.request("GET", path, headers={"Accept-Encoding": "gzip"})
            r = c.getresponse()
            total += len(r.read())
            if r.version == 10 or r.getheader("Connection", "").lower() == "close":
                c.close()
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        res["first_visit_bytes"] = total
        # parallel load: 8 clients x 100 requests
        def client(_):
            return run("/api/health", n=100)["avg_ms"]
        t = time.perf_counter()
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(client, range(8)))
        res["api_health_8x100_rps"] = round(800 / (time.perf_counter() - t))
        return res
    finally:
        proc.terminate()
        proc.wait(5)


# ---------------------------------------------------------------- database
def bench_db():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "d.sqlite3")
    store.init(path)
    db = store.connect(path)
    db.execute("INSERT INTO users(username, pw_hash, pw_salt, created, is_admin) VALUES('u','x','y',0,1)")
    t = time.perf_counter()
    for i in range(500):
        db.execute("INSERT INTO kv VALUES(?, ?)", (f"k{i}", "v"))
    commit_ms = ms((time.perf_counter() - t) / 500)
    db.executemany("INSERT INTO notifications(user_id, kind, title, body, link, at) VALUES(1,'x','t','b','',?)",
                   [(time.time(),) for _ in range(300)])
    t = time.perf_counter()
    for _ in range(200):
        db.execute("SELECT * FROM notifications WHERE user_id=1 ORDER BY id DESC LIMIT 50").fetchall()
        db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=1 AND read=0").fetchone()
    q_ms = ms((time.perf_counter() - t) / 200)
    t = time.perf_counter()
    for _ in range(200):
        store.connect(path).close()
    conn_ms = ms((time.perf_counter() - t) / 200)
    return {"autocommit_write_ms": commit_ms, "notifications_query_ms": q_ms, "open_connection_ms": conn_ms}


if __name__ == "__main__":
    if "--no-net" not in sys.argv:
        OUT["network"] = bench_network()
        OUT["copy_detection"] = bench_stream()
    OUT["server"] = bench_server()
    OUT["database"] = bench_db()
    print(json.dumps(OUT, ensure_ascii=False, indent=1))
