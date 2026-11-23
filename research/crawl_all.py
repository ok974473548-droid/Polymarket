"""Re-crawl the bets of every profiled wallet into an audit copy of the database (app code, 8 threads)."""
import json
import queue
import sys
import threading
import time

from kontur import db as store
from kontur import scout

DB = sys.argv[1]
scout.GAP = 0.06
NOW = time.time()
db0 = store.connect(DB)
db0.execute("CREATE TABLE IF NOT EXISTS audit_meta(k TEXT PRIMARY KEY, v TEXT)")
db0.execute("INSERT OR REPLACE INTO audit_meta VALUES('now', ?)", (str(NOW),))
db0.execute("CREATE TABLE IF NOT EXISTS audit_done(wallet TEXT PRIMARY KEY, n INTEGER, partial INTEGER, error TEXT)")
done_set = {r[0] for r in db0.execute("SELECT wallet FROM audit_done")}
wallets = [r[0] for r in db0.execute("SELECT wallet FROM scout_wallets WHERE metrics IS NOT NULL") if r[0] not in done_set]
db0.close()
q = queue.Queue()
for w in wallets:
    q.put(w)
cnt, lock, t0 = [0], threading.Lock(), time.time()


def work():
    db = store.connect(DB)
    while True:
        try:
            w = q.get_nowait()
        except queue.Empty:
            return
        err, bets, partial = "", [], False
        for attempt in range(3):
            try:
                bets, partial = scout.crawl(w, NOW - scout.CRAWL_DAYS * scout.DAY, NOW)
                err = ""
                break
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:200]
                time.sleep(3)
        with store.tx(db):
            db.execute("DELETE FROM scout_bets WHERE wallet=?", (w,))
            db.executemany("""INSERT OR REPLACE INTO scout_bets(wallet, token, condition, oidx, outcome, title, event_slug, entry, size,
                              opened, closed, exit, state, mark, end_ts, short) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           [(w, b["token"], b["condition"], b["oidx"], b["outcome"], b["title"], b["event_slug"], b["entry"],
                             b["size"], b["opened"], b["closed"], b["exit"], b["state"], b["mark"], b["end"], b["short"]) for b in bets])
            db.execute("INSERT OR REPLACE INTO audit_done VALUES(?,?,?,?)", (w, len(bets), int(partial), err))
        with lock:
            cnt[0] += 1
            if cnt[0] % 100 == 0:
                print(f"{cnt[0]}/{len(wallets)} {time.time() - t0:.0f}s", flush=True)


ts = [threading.Thread(target=work) for _ in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print(f"DONE {cnt[0]}/{len(wallets)} {time.time() - t0:.0f}s", flush=True)
