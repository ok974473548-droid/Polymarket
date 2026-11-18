"""Задачи сбора данных (раздел 4 ТЗ): синхронизация рынков, сделок, стаканов, цен.

Клиенты передаются параметрами (duck typing), поэтому в тестах и демо вместо
реального Polymarket подставляется синтетический рынок.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Iterable

from ..db import DB, now_ts
from ..util import Progress, chunks, pmap
from .polymarket import GammaClient, parse_activity, parse_market, parse_trade

WORKERS = 8           # параллельных запросов к одному API (частоту дополнительно ограничивает клиент)
CLOB_WORKERS = 16     # цены и стаканы: лимит CLOB выше (1000–1500 запросов за 10 с)
MARKET_BATCH = 50     # рынков в одном запросе к каталогу
POLL_OVERLAP_S = 900  # опрос кошелька начинается на 15 мин раньше последней известной сделки

log = logging.getLogger(__name__)


def sync_markets(db: DB, gamma, closed: bool | None = None, **filters) -> int:
    """sync_markets / sync_resolutions: каталог рынков, токены, исходы."""
    n = 0
    with db.job("sync_markets") as job:
        markets, tokens, events = [], [], {}
        kw = {"workers": 6} if isinstance(gamma, GammaClient) else {}     # настоящий клиент — страницы параллельно
        for raw in gamma.markets(closed=closed, **kw, **filters):
            m, toks, ev = parse_market(raw)
            if not m["market_id"]:
                continue
            markets.append(m)
            tokens.extend(toks)
            if ev and ev["event_id"]:
                events[ev["event_id"]] = ev
        with db.tx():
            db.upsert_many("events", list(events.values()), ["event_id"])
            db.upsert_many("markets", markets, ["market_id"])
            db.upsert_many("tokens", tokens, ["token_id"])
        n = len(markets)
        job["rows"] = n
    return n


def _known(db: DB, table: str, col: str, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for chunk in chunks(ids, 500):
        ph = ",".join("?" for _ in chunk)
        out.update(r[col] for r in db.query(f"SELECT {col} FROM {table} WHERE {col} IN ({ph})", chunk))
    return out


def fetch_markets(gamma, market_ids: list[str], progress: Progress | None = None) -> list[dict]:
    """Сырые рынки из каталога: пачками по 50 (если клиент умеет) и параллельно;
    то, что не пришло пачкой, — по одному, тоже параллельно."""
    ids = list(dict.fromkeys(m for m in market_ids if m))
    if not ids:
        return []
    got: dict[str, dict] = {}
    total = len(ids)

    def tick(extra=0):
        if progress:
            progress("markets", min(len(got) + extra, total), total)

    if hasattr(gamma, "markets_by_ids"):
        for _, res, exc in pmap(gamma.markets_by_ids, chunks(ids, MARKET_BATCH), WORKERS,
                                on_done=lambda d, n: tick()):
            if exc:
                log.warning("рынки пачкой: %s", exc)
                continue
            for raw in res or []:
                cid = str(raw.get("conditionId") or raw.get("condition_id") or "").lower()
                got[cid] = raw
            tick()
    rest = [m for m in ids if m.lower() not in got]
    if rest:
        for mid, raw, exc in pmap(gamma.market, rest, WORKERS, on_done=lambda d, n: tick(d)):
            if exc:
                log.warning("рынок %s: %s", mid, exc)
            elif raw:
                got[mid.lower()] = raw
    if progress:
        progress("markets", total, total)          # все запрошены (несуществующие просто пропущены)
    return list(got.values())


def store_markets(db: DB, raws: list[dict]) -> int:
    markets, tokens, events = [], [], {}
    for raw in raws:
        m, toks, ev = parse_market(raw)
        if not m["market_id"]:
            continue
        markets.append(m)
        tokens.extend(toks)
        if ev and ev["event_id"]:
            events[ev["event_id"]] = ev
    with db.tx():
        db.upsert_many("events", list(events.values()), ["event_id"])
        db.upsert_many("markets", markets, ["market_id"])
        db.upsert_many("tokens", tokens, ["token_id"])
    return len(markets)


def ensure_markets(db: DB, gamma, market_ids: Iterable[str], progress: Progress | None = None) -> int:
    """Догружает рынки, которых нет в каталоге (встретились в сделках)."""
    ids = list({m for m in market_ids if m})
    known = _known(db, "markets", "market_id", ids)
    missing = [m for m in ids if m not in known]
    if not missing:
        return 0
    return store_markets(db, fetch_markets(gamma, missing, progress))


def _existing_ids(db: DB, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        out.update(r["trade_id"] for r in db.query(f"SELECT trade_id FROM trades WHERE trade_id IN ({ph})", chunk))
    return out


_STORE_LOCK = threading.Lock()     # поток сделок и опрос пишут параллельно: «новая» сделка отдаётся ровно один раз


def natural_key(t: dict) -> tuple | None:
    """Сделка по существу: транзакция, кошелёк, исход, сторона, объём, цена. Поток (WebSocket) и опрос (REST)
    отдают одну и ту же сделку с разной записью чисел ("100" и 100.0) — по этому ключу они сводятся в одну."""
    if not t.get("tx_hash"):
        return None
    return (str(t["tx_hash"]).lower(), t["wallet"], str(t["token_id"]), t["side"],
            round(float(t["size_shares"]), 4), round(float(t["price"]), 4))


def _existing_natural(db: DB, trades: list[dict]) -> set:
    hashes = list({t["tx_hash"] for t in trades if t.get("tx_hash")})
    hashes += [h.lower() for h in hashes if h.lower() != h]
    out: set = set()
    for chunk in chunks(hashes, 500):
        ph = ",".join("?" for _ in chunk)
        for r in db.query(f"SELECT tx_hash, wallet, token_id, side, size_shares, price FROM trades "
                          f"WHERE tx_hash IN ({ph})", chunk):
            out.add(natural_key(r))
    return out


def store_trades(db: DB, raw_rows: Iterable[dict], fetched_ts: int | None = None) -> list[dict]:
    """Идемпотентная запись сделок. Возвращает только новые (по id и по существу сделки)."""
    parsed: dict = {}
    seen: set = set()
    for r in raw_rows:
        t = parse_trade(r, fetched_ts)
        k = natural_key(t)
        if k is not None and k in seen and t["trade_id"] not in parsed:
            continue
        if k is not None:
            seen.add(k)
        parsed[t["trade_id"]] = t
    if not parsed:
        return []
    with _STORE_LOCK:
        known = _existing_ids(db, list(parsed))
        new = [t for tid, t in parsed.items() if tid not in known]
        if new:
            nat = _existing_natural(db, new)
            new = [t for t in new if natural_key(t) is None or natural_key(t) not in nat]
        if new:
            with db.tx():
                db.insert_ignore("trades", new)
                _touch_wallets(db, new)
    return sorted(new, key=lambda t: t["ts"])


def _touch_wallets(db: DB, trades: list[dict]) -> None:
    first: dict[str, int] = {}
    for t in trades:
        first[t["wallet"]] = min(first.get(t["wallet"], t["ts"]), t["ts"])
    if not first:
        return
    known = {}
    for chunk in chunks(list(first), 500):
        ph = ",".join("?" for _ in chunk)
        known.update({r["wallet"]: r["first_seen_ts"] for r in
                      db.query(f"SELECT wallet, first_seen_ts FROM wallets WHERE wallet IN ({ph})", chunk)})
    new = [{"wallet": w, "owner_eoa": None, "is_proxy": 1, "first_seen_ts": ts, "first_funded_ts": None,
            "funder_wallet": None} for w, ts in first.items() if w not in known]
    db.upsert_many("wallets", new, ["wallet"])
    older = [(ts, w) for w, ts in first.items() if w in known and (known[w] is None or ts < known[w])]
    if older:
        db.executemany("UPDATE wallets SET first_seen_ts=? WHERE wallet=?", older)


def ingest_wallet_trades(db: DB, data, wallets: Iterable[str], gamma=None, full_history: bool = False,
                         max_pages: int | None = None, workers: int = 1,
                         progress: Progress | None = None) -> list[dict]:
    """ingest_trades по списку кошельков: догружает сделки с последней известной.
    workers > 1 — истории разных кошельков загружаются параллельно (страницы одного — по очереди)."""
    wallets = list(dict.fromkeys(w.lower() for w in wallets))
    starts = {}
    for w in wallets:
        last = None if full_history else db.scalar("SELECT MAX(ts) AS m FROM trades WHERE wallet=?", (w,))
        # с запасом назад: сделка из потока WebSocket могла прийти раньше опроса, а сделки до неё
        # (например, исполненные заявки-мейкеры, которых нет в ленте) ещё не загружены. Повторы отсекаются.
        starts[w] = max(1, last - POLL_OVERLAP_S) if last else 1
    kw = {"max_pages": max_pages} if max_pages else {}
    seen = {"n": 0}

    def fetch(w):
        rows = []
        for r in data.trades(user=w, start=starts[w], **kw):
            rows.append(r)
            if progress and len(rows) % 1000 == 0:
                progress("trades", seen["n"] + len(rows), 0)
        seen["n"] += len(rows)
        return rows

    new_all: list[dict] = []
    with db.job("ingest_trades") as job:
        for w, rows, exc in pmap(fetch, wallets, workers):
            if exc:
                if len(wallets) == 1:
                    raise exc
                log.warning("сделки %s: %s", w, exc)
                continue
            new_all.extend(store_trades(db, rows, now_ts()))
        if progress:
            progress("trades", seen["n"], seen["n"])
        if gamma is not None:
            ensure_markets(db, gamma, (t["market_id"] for t in new_all), progress)
        job["rows"] = len(new_all)
    return new_all


def ingest_global_trades(db: DB, data, pages: int = 5, gamma=None) -> list[dict]:
    """Глобальная лента последних сделок — источник для поиска новых кандидатов."""
    rows = []
    for i, r in enumerate(data.trades(limit=1000)):
        rows.append(r)
        if i >= pages * 1000:
            break
    new = store_trades(db, rows, now_ts())
    if gamma is not None:
        ensure_markets(db, gamma, (t["market_id"] for t in new))
    return new


def poll_feed(db: DB, data, wallets: set[str], gamma=None) -> list[dict]:
    """Одна страница общей ленты (с мейкерами) → новые сделки отслеживаемых кошельков."""
    rows = [r for r in data.trades(limit=1000, max_pages=1, taker_only=False)
            if str(r.get("proxy_wallet", "")).lower() in wallets]
    new = store_trades(db, rows, now_ts())
    if gamma is not None and new:
        ensure_markets(db, gamma, (t["market_id"] for t in new))
    return new


def refresh_markets(db: DB, gamma, market_ids: Iterable[str]) -> int:
    """Обновляет конкретные рынки (статус, исход) — для тех, где у нас или у китов есть позиции."""
    return store_markets(db, fetch_markets(gamma, list(set(market_ids))))


def discover_wallets(db: DB, data, gamma=None, pages: int = 5, max_new: int = 50,
                     min_volume_usd: float = 1000.0, max_history_pages: int = 50) -> list[str]:
    """Поиск кандидатов: самые активные кошельки свежей ленты, чья полная история ещё не загружена."""
    vol: dict[str, float] = {}
    for r in data.trades(limit=1000, max_pages=pages):
        w = str(r.get("proxy_wallet", "")).lower()
        vol[w] = vol.get(w, 0.0) + float(r["price"]) * float(r["size"])
    loaded = {r["wallet"] for r in db.query("SELECT wallet FROM wallet_sync WHERE full_loaded_ts IS NOT NULL")}
    picked = [w for w, v in sorted(vol.items(), key=lambda kv: -kv[1]) if v >= min_volume_usd and w not in loaded][:max_new]
    # не больше 50 тыс. сделок на кандидата: огромные истории — почти всегда маркет-мейкеры.
    # Истории кандидатов грузятся параллельно, рынки — одной пачкой на всех.
    ingest_wallet_trades(db, data, picked, gamma=gamma, full_history=True, max_pages=max_history_pages, workers=4)
    try:
        sync_activities(db, data, picked, gamma=gamma)
    except Exception as exc:  # noqa: BLE001 — кандидаты сохраняются и без журнала операций
        log.warning("операции кандидатов: %s", exc)
    db.upsert_many("wallet_sync", [{"wallet": w, "full_loaded_ts": now_ts(), "feed_volume_usd": vol[w]}
                                   for w in picked], ["wallet"])
    return picked


def missing_trade_markets(db: DB, limit: int = 3000, wallets: list[str] | None = None) -> list[str]:
    """Рынки, по которым есть сделки, но нет записи в каталоге (например, закрытые рынки,
    не найденные до v0.7.1). Без них у позиций нет исхода и темы."""
    where, params = "", []
    if wallets:
        where = f"AND t.wallet IN ({','.join('?' for _ in wallets)})"
        params = list(wallets)
    rows = db.query(f"SELECT t.market_id, MAX(t.ts) AS m FROM trades t WHERE t.market_id IS NOT NULL {where} "
                    "AND NOT EXISTS (SELECT 1 FROM markets k WHERE k.market_id=t.market_id) "
                    "GROUP BY t.market_id ORDER BY m DESC LIMIT ?", params + [limit])
    return [r["market_id"] for r in rows]


def tokens_missing_prices(db: DB, limit: int = 300, wallets: list[str] | None = None) -> list[str]:
    """Токены из сделок без истории цен (CLV без неё не посчитать) — сначала самые свежие.
    Берём из trades, а не из positions: позиции строятся позже, в ежедневном пересчёте.
    wallets — только сделки этих кошельков (киты из списка)."""
    where, params = "", []
    if wallets:
        where = f"AND t.wallet IN ({','.join('?' for _ in wallets)})"
        params = list(wallets)
    rows = db.query("SELECT t.token_id, MAX(t.ts) AS m FROM trades t WHERE NOT EXISTS "
                    f"(SELECT 1 FROM price_history h WHERE h.token_id=t.token_id) {where} GROUP BY t.token_id "
                    "ORDER BY m DESC LIMIT ?", params + [limit])
    return [r["token_id"] for r in rows]


# ---------------------------------------------------------------- стаканы и цены

def store_book(db: DB, token_id: str, bids, asks, ts: int | None = None, trigger: str = "schedule") -> None:
    db.upsert("orderbook_snapshots", {
        "token_id": token_id, "ts": ts or now_ts(),
        "bids": json.dumps(list(bids)[:10]), "asks": json.dumps(list(asks)[:10]), "trigger": trigger,
    }, ["token_id", "ts"])


def snapshot_books(db: DB, clob, token_ids: Iterable[str], trigger: str = "schedule", ts: int | None = None) -> dict:
    """Снимки стаканов: запросы параллельно, запись одной транзакцией."""
    out = {}
    res = pmap(clob.book, list(token_ids), CLOB_WORKERS)
    with db.tx():
        for tid, book, exc in res:
            if exc:  # стакан одного рынка не должен ронять задачу
                log.warning("book %s: %s", tid, exc)
                continue
            bids, asks = book
            store_book(db, tid, bids, asks, ts, trigger)
            out[tid] = (bids, asks)
    return out


def latest_book(db: DB, token_id: str, at_ts: int | None = None, max_age_s: int | None = None):
    """Последний снимок стакана не позже at_ts (для бэктеста — без заглядывания в будущее)."""
    at_ts = at_ts if at_ts is not None else 2**62
    row = db.one("SELECT ts, bids, asks FROM orderbook_snapshots WHERE token_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                 (token_id, at_ts))
    if not row:
        return None
    if max_age_s is not None and at_ts - row["ts"] > max_age_s:
        return None
    return row["ts"], [tuple(x) for x in json.loads(row["bids"])], [tuple(x) for x in json.loads(row["asks"])]


def sync_prices(db: DB, clob, token_ids: Iterable[str], start: int | None = None, end: int | None = None,
                fidelity_min: int = 1, progress: Progress | None = None) -> int:
    """История цен: запросы параллельно, запись в вызывающем потоке."""
    n = 0
    tokens = list(dict.fromkeys(token_ids))
    with db.job("sync_prices") as job:
        res = pmap(lambda t: clob.prices_history(t, start, end, fidelity_min=fidelity_min), tokens, CLOB_WORKERS,
                   on_done=(lambda d, k: progress("prices", d, k)) if progress else None)
        rows = []
        for tid, pts, exc in res:
            if exc:
                log.warning("prices %s: %s", tid, exc)
                continue
            rows.extend({"token_id": tid, "ts": t, "price": p} for t, p in pts)
        n = db.insert_ignore("price_history", rows)
        job["rows"] = n
    return n


def price_at(db: DB, token_id: str, ts: int, max_age_s: int | None = 6 * 3600) -> float | None:
    row = db.one("SELECT ts, price FROM price_history WHERE token_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                 (token_id, ts))
    if not row or (max_age_s is not None and ts - row["ts"] > max_age_s):
        return None
    return float(row["price"])


def data_freshness_s(db: DB) -> int | None:
    """Сколько секунд назад пришли последние данные (индикатор «Система работает»)."""
    last = db.scalar("SELECT MAX(ts_fetched) AS m FROM trades")
    return None if last is None else max(0, now_ts() - int(last))


def wait(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------- журнал операций кошелька
def sync_activities(db: DB, data, wallets: Iterable[str], gamma=None, workers: int = 4,
                    progress: Progress | None = None) -> int:
    """split / merge / conversion из /activity: с последней загруженной операции.
    Без них позиции китов, которые делят и сливают токены, считаются неверно."""
    if not hasattr(data, "activity"):
        return 0
    wallets = list(dict.fromkeys(w.lower() for w in wallets))
    if not wallets:
        return 0
    last = {r["wallet"]: r["last_ts"] for r in db.query(
        f"SELECT wallet, last_ts FROM activity_sync WHERE wallet IN ({','.join('?' for _ in wallets)})", wallets)}
    if progress:
        progress("activity", 0, len(wallets))

    def fetch(w):
        return [parse_activity(r) for r in data.activity(w, start=last.get(w))]

    n, now = 0, now_ts()
    rows_all = []
    for w, rows, exc in pmap(fetch, wallets, workers,
                             on_done=(lambda d, k: progress("activity", d, k)) if progress else None):
        if exc:
            log.warning("операции %s: %s", w, exc)
            db.upsert("activity_sync", {"wallet": w, "fetched_ts": now, "last_ts": last.get(w), "error": repr(exc)[:200]},
                      ["wallet"])
            continue
        rows = _new_activities(db, rows)
        n += db.insert_ignore("activities", rows)
        rows_all += rows
        top = max([r["ts"] for r in rows] + [last.get(w) or 0])
        db.upsert("activity_sync", {"wallet": w, "fetched_ts": now, "last_ts": top or None, "error": None}, ["wallet"])
    if gamma is not None and rows_all:
        ensure_markets(db, gamma, (r["market_id"] for r in rows_all if r["market_id"]))
    apply_activities(db, wallets)
    return n


def _act_key(a: dict) -> tuple:
    return (a["wallet"], str(a.get("tx_hash") or "").lower(), str(a["type"]).upper(), a.get("market_id") or "",
            str(a.get("token_id") or ""), round(float(a.get("size") or 0), 6), int(a["ts"]))


def _new_activities(db: DB, rows: list[dict]) -> list[dict]:
    """Отсев операций, уже записанных раньше под другим ключом (старый метод /activity писал числа иначе) —
    иначе split/merge применились бы дважды и позиции кита разъехались."""
    hashes = list({str(r["tx_hash"]).lower() for r in rows if r.get("tx_hash")})
    wallets = list({r["wallet"] for r in rows})
    known: set = set()
    for chunk in chunks(hashes, 400):
        for a in db.query(f"SELECT * FROM activities WHERE wallet IN ({','.join('?' for _ in wallets)}) "
                          f"AND LOWER(tx_hash) IN ({','.join('?' for _ in chunk)})", wallets + chunk):
            known.add(_act_key(a))
    out, seen = [], set()
    for r in rows:
        k = _act_key(r)
        if k not in known and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def apply_activities(db: DB, wallets: list[str] | None = None) -> int:
    """split → покупка всех исходов рынка по 1/n, merge → продажа всех исходов по 1/n
    (в сумме ровно $1 за комплект — так и есть в контракте). Записи помечаются source, агент их не копирует."""
    where, params = "", []
    if wallets:
        where = f"AND a.wallet IN ({','.join('?' for _ in wallets)})"
        params = list(wallets)
    acts = db.query(f"SELECT * FROM activities a WHERE a.type IN ('SPLIT','MERGE') {where}", params)
    if not acts:
        return 0
    mids = list({a["market_id"] for a in acts if a["market_id"]})
    toks: dict[str, list[str]] = {}
    for chunk in chunks(mids, 500):
        for r in db.query(f"SELECT token_id, market_id FROM tokens WHERE market_id IN ({','.join('?' for _ in chunk)})",
                          chunk):
            toks.setdefault(r["market_id"], []).append(r["token_id"])
    rows, now = [], now_ts()
    for a in acts:
        ts_ = toks.get(a["market_id"]) or []
        if len(ts_) < 2 or not a["size"]:
            continue                                   # рынок ещё не загружен — применим при следующем проходе
        px = 1.0 / len(ts_)
        side = "BUY" if a["type"] == "SPLIT" else "SELL"
        for t in ts_:
            rows.append({"trade_id": f"act:{a['activity_id']}:{t}", "ts": a["ts"], "ts_fetched": now,
                         "wallet": a["wallet"], "token_id": t, "market_id": a["market_id"], "side": side,
                         "price": px, "size_shares": a["size"], "size_usd": a["size"] * px, "is_maker": None,
                         "tx_hash": a["tx_hash"], "source": a["type"].lower()})
    return db.insert_ignore("trades", rows)


def conversion_events(db: DB, wallets: list[str] | None = None) -> set[tuple[str, str]]:
    """(кошелёк, событие), где были конвертации neg-risk: позиции там восстановить точно нельзя —
    они исключаются из оценки игрока."""
    where, params = "", []
    if wallets:
        where = f"AND a.wallet IN ({','.join('?' for _ in wallets)})"
        params = list(wallets)
    rows = db.query("SELECT DISTINCT a.wallet, COALESCE(m.event_id, a.market_id) AS ev FROM activities a "
                    f"LEFT JOIN markets m ON m.market_id=a.market_id WHERE a.type='CONVERSION' {where}", params)
    return {(r["wallet"], r["ev"]) for r in rows if r["ev"]}
