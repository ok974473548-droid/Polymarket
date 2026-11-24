"""Copy trading on virtual profiles, modelled on polymarketanalytics.com/copy-trading.

A profile is a separate virtual balance. Each copy trade points the profile at one public
wallet. Every new fill of that wallet is repeated at the chosen size (fixed dollars or a
percentage of the leader's trade) within the spending limits, price range and slippage.
Leader sells are mirrored proportionally: if the leader sells 40% of a position, the profile
sells 40% of its own.
"""

import json
import threading
import time
from datetime import datetime, timezone

from . import bus
from . import db as store
from . import notify
from . import polymarket as pm
from . import trading

MIN_COPY = 1.0
COPY_MAX_AGE = 15 * 60  # a buy older than this (seen late by the polling fallback, e.g. after a restart) is not copied


def _day_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- profiles

def _autopilot_ids(db):
    return {int(r[0]) for r in db.execute("SELECT value FROM kv WHERE key LIKE 'ap:%:account'")}


def profiles(db, uid):
    """Copy profiles of the user; the autopilot's own profile is managed on its page."""
    out, skip = [], _autopilot_ids(db)
    for (aid,) in db.execute("SELECT id FROM accounts WHERE user_id=? AND kind='copy' ORDER BY id", (uid,)).fetchall():
        if aid not in skip:
            out.append(trading.summary(db, uid, aid))
    return out


def create_profile(db, uid, name):
    name = str(name or "").strip()[:40] or "Профиль копирования"
    with store.tx(db):
        cur = db.execute("INSERT INTO accounts(user_id, kind, name, balance, created) VALUES(?,?,?,0,?)",
                         (uid, "copy", name, time.time()))
    return trading.summary(db, uid, cur.lastrowid)


def _copy_account(db, uid, aid, allow_autopilot=False):
    acc = trading.owned_account(db, uid, aid)
    if acc["kind"] != "copy":
        raise ValueError("Это не профиль копирования")
    if not allow_autopilot and acc["id"] in _autopilot_ids(db):
        raise ValueError("Профилем автопилота управляет страница «Автопилот»")
    return acc


def rename_profile(db, uid, aid, name):
    _copy_account(db, uid, aid)
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("Введите название профиля")
    db.execute("UPDATE accounts SET name=? WHERE id=?", (name, aid))
    return trading.summary(db, uid, aid)


def pause_profile(db, uid, aid, paused):
    _copy_account(db, uid, aid)
    db.execute("UPDATE accounts SET paused=? WHERE id=?", (int(bool(paused)), aid))
    return trading.summary(db, uid, aid)


def delete_profile(db, uid, aid):
    acc = _copy_account(db, uid, aid)
    if db.execute("SELECT 1 FROM positions WHERE account_id=? AND status='open' AND shares>0.000001", (aid,)).fetchone():
        raise ValueError("Сначала продайте открытые позиции профиля")
    main = trading.main_account(db, uid)
    with store.tx(db):
        for order in store.rows(db.execute("SELECT * FROM orders WHERE account_id=? AND status='open'", (aid,))):
            trading._release(db, order, "Профиль удалён", "cancelled")
        bal = db.execute("SELECT balance FROM accounts WHERE id=?", (aid,)).fetchone()[0]
        if bal > 0:
            trading._move(db, main["id"], bal, "transfer_in", f"Остаток удалённого профиля «{acc['name']}»")
        db.execute("DELETE FROM accounts WHERE id=?", (aid,))
    return {"deleted": aid}


# ---------------------------------------------------------------- copy trades

def _price(value):
    v = pm.num(value)
    if v is None:
        return None
    if not 0 <= v <= 1:
        raise ValueError("Диапазон цен задаётся в долларах от $0 до $1")
    return v


def _validate(body):
    leader = pm.address(body.get("leader"))
    sizing = body.get("sizing")
    if sizing not in {"fixed", "percent"}:
        raise ValueError("Выберите способ расчёта размера: фиксированная сумма или процент")
    out = {"leader": leader, "nickname": str(body.get("nickname") or "").strip()[:60], "sizing": sizing,
           "amount": None, "percent": None}
    if sizing == "fixed":
        amount = pm.num(body.get("amount"))
        if amount is None or amount < 1.1 or amount > 1e7:
            raise ValueError("Сумма на сделку: от $1,10 до $10 000 000")
        out["amount"] = round(amount, 2)
    else:
        pct = pm.num(body.get("percent"))
        if pct is None or not 1 <= pct <= 1000:
            raise ValueError("Процент от сделки лидера: от 1% до 1000%")
        out["percent"] = pct
    for key, name in (("max_per_trade", "Лимит на сделку"), ("daily_limit", "Дневной лимит")):
        v = pm.num(body.get(key))
        if v is not None and v <= 0:
            raise ValueError(f"{name}: укажите положительную сумму или «Без лимита»")
        out[key] = v
    out["price_min"], out["price_max"] = _price(body.get("price_min")), _price(body.get("price_max"))
    if out["price_min"] is not None and out["price_max"] is not None and out["price_min"] >= out["price_max"]:
        raise ValueError("Нижняя граница цены должна быть меньше верхней")
    slip = body.get("slippage")
    if slip in (None, "", "any"):
        out["slippage"] = None
    else:
        s = pm.num(slip)
        if s is None or not 0 <= s <= 50:
            raise ValueError("Проскальзывание: от 0% до 50%")
        out["slippage"] = s / 100
    out["notify"] = int(body.get("notify", True) is not False)
    return out


def _latest_cursor(leader):
    try:
        page = pm.trades_page(user=leader, limit=20, taker_only=False)
    except (pm.UpstreamError, LookupError, ValueError):
        return int(time.time()), []
    rows = page["rows"]
    ts = max((int(r["timestamp"] or 0) for r in rows), default=int(time.time()))
    keys = [_key(r) for r in rows if int(r["timestamp"] or 0) == ts]
    return ts, keys


def _key(r):
    return f"{r.get('transaction_hash')}:{r.get('token_id')}:{r.get('side')}:{r.get('size')}"


def create(db, uid, aid, body):
    _copy_account(db, uid, aid)
    v = _validate(body)
    ts, keys = _latest_cursor(v["leader"])
    with store.tx(db):
        cur = db.execute("""INSERT INTO copy_trades(account_id, leader, nickname, sizing, amount, percent, max_per_trade,
                            daily_limit, price_min, price_max, slippage, notify, cursor_ts, cursor_keys, created)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (aid, v["leader"], v["nickname"], v["sizing"], v["amount"], v["percent"], v["max_per_trade"],
                          v["daily_limit"], v["price_min"], v["price_max"], v["slippage"], v["notify"], ts,
                          json.dumps(keys), time.time()))
    return get(db, uid, cur.lastrowid)


def _owned(db, uid, cid):
    row = store.one(db.execute("""SELECT c.* FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                  WHERE c.id=? AND a.user_id=?""", (int(cid), uid)))
    if not row:
        raise LookupError("Копирование не найдено")
    return row


def update(db, uid, cid, body):
    row = _owned(db, uid, cid)
    v = _validate({**row, "slippage": None if row["slippage"] is None else row["slippage"] * 100, **body, "leader": row["leader"]})
    db.execute("""UPDATE copy_trades SET nickname=?, sizing=?, amount=?, percent=?, max_per_trade=?, daily_limit=?,
                  price_min=?, price_max=?, slippage=?, notify=? WHERE id=?""",
               (v["nickname"], v["sizing"], v["amount"], v["percent"], v["max_per_trade"], v["daily_limit"],
                v["price_min"], v["price_max"], v["slippage"], v["notify"], row["id"]))
    return get(db, uid, row["id"])


def set_paused(db, uid, cid, paused):
    row = _owned(db, uid, cid)
    if not paused:
        # Resuming skips trades made while paused, as on the reference service.
        ts, keys = _latest_cursor(row["leader"])
        db.execute("UPDATE copy_trades SET paused=0, cursor_ts=?, cursor_keys=? WHERE id=?", (ts, json.dumps(keys), row["id"]))
    else:
        db.execute("UPDATE copy_trades SET paused=1 WHERE id=?", (row["id"],))
    return get(db, uid, row["id"])


def remove(db, uid, cid):
    row = _owned(db, uid, cid)
    db.execute("DELETE FROM copy_trades WHERE id=?", (row["id"],))
    return {"deleted": row["id"]}


def _public(db, row):
    today = _day_start()
    stats = db.execute("""SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='filled' AND side='buy' AND at>=? THEN amount ELSE 0 END),0)
                          FROM copy_events WHERE copy_trade_id=?""", (today, row["id"])).fetchone()
    spent = db.execute("""SELECT COALESCE(SUM(filled_cost + fees),0) FROM orders WHERE copy_trade_id=? AND side='buy'""",
                       (row["id"],)).fetchone()[0]
    got = db.execute("""SELECT COALESCE(SUM(filled_cost - fees),0) FROM orders WHERE copy_trade_id=? AND side='sell'""",
                     (row["id"],)).fetchone()[0]
    name = db.execute("SELECT name, image FROM wallets WHERE wallet=?", (row["leader"],)).fetchone()
    return {**{k: row[k] for k in ("id", "leader", "nickname", "sizing", "amount", "percent", "max_per_trade", "daily_limit",
                                    "price_min", "price_max", "slippage", "created", "last_check", "last_error")},
            "account": row["account_id"], "notify": bool(row["notify"]), "paused": bool(row["paused"]),
            "leader_name": name["name"] if name else "", "leader_image": name["image"] if name else "",
            "events": stats[0], "filled": stats[1], "spent_today": stats[2], "spent": spent, "received": got}


def get(db, uid, cid):
    return _public(db, _owned(db, uid, cid))


def listing(db, uid, aid):
    _copy_account(db, uid, aid)
    return [_public(db, r) for r in store.rows(db.execute("SELECT * FROM copy_trades WHERE account_id=? ORDER BY id DESC", (aid,)))]


EVENT_FIELDS = ("id", "copy_trade_id", "account_id", "leader", "status", "reason", "side", "leader_price", "leader_size",
                "amount", "shares", "price", "order_id", "leader_tx", "at", "source", "leader_ts", "detected")


def events(db, uid, aid, status="", limit=200):
    _copy_account(db, uid, aid, allow_autopilot=True)
    # Market texts come from `outcomes` (1.3.0); older rows keep their own copy.
    sql = (f"SELECT {', '.join('e.' + f for f in EVENT_FIELDS)}, "
           + ", ".join(f"CASE WHEN e.outcome_id IS NULL THEN e.{c} ELSE COALESCE(o.{c}, '') END AS {c}"
                       for c in ("title", "outcome", "event_slug", "image"))
           + ", r.fees AS fee FROM copy_events e LEFT JOIN outcomes o ON o.id=e.outcome_id"
             " LEFT JOIN orders r ON r.id=e.order_id WHERE e.account_id=?")
    params = [aid]
    if status in {"filled", "skipped", "failed"}:
        sql += " AND e.status=?"
        params.append(status)
    return store.rows(db.execute(sql + " ORDER BY e.id DESC LIMIT ?", (*params, limit)))


def outcome_id(db, trade):
    """Id of the outcome's texts in `outcomes`, added or completed on the way (None without a token)."""
    tok = str(trade.get("token_id") or "")
    if not tok:
        return None
    vals = (str(trade.get("title") or "")[:300], str(trade.get("outcome") or "")[:120], str(trade.get("event_slug") or "")[:240],
            str(trade.get("icon") or "")[:600])
    db.execute("""INSERT INTO outcomes(token, title, outcome, event_slug, image) VALUES(?,?,?,?,?)
                  ON CONFLICT(token) DO UPDATE SET
                    title=CASE WHEN outcomes.title='' THEN excluded.title ELSE outcomes.title END,
                    outcome=CASE WHEN outcomes.outcome='' THEN excluded.outcome ELSE outcomes.outcome END,
                    event_slug=CASE WHEN outcomes.event_slug='' THEN excluded.event_slug ELSE outcomes.event_slug END,
                    image=CASE WHEN outcomes.image='' THEN excluded.image ELSE outcomes.image END""", (tok, *vals))
    return db.execute("SELECT id FROM outcomes WHERE token=?", (tok,)).fetchone()[0]


# ---------------------------------------------------------------- engine

def _record(db, ct, status, reason, trade, *, source="poll", detected=None, **extra):
    oid = outcome_id(db, trade)
    texts = ("", "", "", "") if oid else (trade.get("title") or "", trade.get("outcome") or "", trade.get("event_slug") or "",
                                          trade.get("icon") or "")
    db.execute("""INSERT INTO copy_events(copy_trade_id, account_id, leader, status, reason, side, title, outcome, event_slug,
                  image, leader_price, leader_size, amount, shares, price, order_id, leader_tx, at, key_hash, outcome_id, source,
                  leader_ts, detected)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (ct["id"], ct["account_id"], ct["leader"], status, reason, (trade.get("side") or "").lower(), *texts,
                trade.get("price"), trade.get("size"), extra.get("amount"), extra.get("shares"), extra.get("price"),
                extra.get("order_id"), trade.get("transaction_hash") or "", time.time(), store.key_hash(trade_key(trade)), oid,
                source, pm.num(trade.get("timestamp")), detected))
    bus.publish("copy", {"account_id": ct["account_id"], "status": status})
    if ct["notify"] and status != "skipped":
        uid = db.execute("SELECT user_id FROM accounts WHERE id=?", (ct["account_id"],)).fetchone()[0]
        who = ct["nickname"] or (ct["leader"][:6] + "…" + ct["leader"][-4:])
        verb = "покупка" if (trade.get("side") or "").upper() == "BUY" else "продажа"
        if status == "filled":
            title = f"Скопирована {verb}: {who}"
            body = f"{trade.get('title')} · {trade.get('outcome')} · ${extra.get('amount') or 0:,.2f}"
        else:
            title = f"Копия не выполнена: {who}"
            body = f"{trade.get('title')} · {reason}"
        notify.add(db, uid, "copy", title, body, "/copy-trading?tab=activity")


def _spent_today(db, ct, condition_id=None):
    sql = """SELECT COALESCE(SUM(o.filled_cost + o.fees),0) FROM orders o WHERE o.copy_trade_id=? AND o.side='buy'
             AND o.created >= ?"""
    params = [ct["id"], _day_start()]
    if condition_id:
        sql += " AND o.condition_id=?"
        params.append(condition_id)
    return db.execute(sql, params).fetchone()[0]


def _copy_buy(db, uid, ct, trade, info):
    price, size = trade["price"], trade["size"]
    if price is None or size is None:
        return "skipped", "Сделка лидера без цены", {}
    if ct["price_min"] is not None and price < ct["price_min"] - 1e-9:
        return "skipped", f"Цена ${price:.2f} ниже диапазона", {}
    if ct["price_max"] is not None and price > ct["price_max"] + 1e-9:
        return "skipped", f"Цена ${price:.2f} выше диапазона", {}
    leader_amount = price * size
    amount = ct["amount"] if ct["sizing"] == "fixed" else leader_amount * ct["percent"] / 100
    if ct["max_per_trade"] is not None:
        amount = min(amount, ct["max_per_trade"] - _spent_today(db, ct, info["condition"]))
    if ct["daily_limit"] is not None:
        left = ct["daily_limit"] - _spent_today(db, ct)
        if left < MIN_COPY:
            return "skipped", "Достигнут дневной лимит", {}
        amount = min(amount, left)
    cash = db.execute("SELECT balance FROM accounts WHERE id=?", (ct["account_id"],)).fetchone()[0]
    if amount > cash:
        amount = cash
        if amount < MIN_COPY:
            return "skipped", "Недостаточно средств в профиле", {}
    amount = round(amount, 2)
    if amount < MIN_COPY:
        return "skipped", "Размер копии меньше минимума $1", {}
    cap = None if ct["slippage"] is None else min(0.999, price * (1 + ct["slippage"]))
    try:
        order = trading.place(db, uid, {"side": "buy", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "amount": amount, "max_price": cap},
                              account_id=ct["account_id"], source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = "Цена ушла дальше допустимого проскальзывания" if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"amount": amount}
    return "filled", "", {"amount": round(order["filled"] * (order["avg"] or 0) + order["fees"], 6),
                          "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


def sell_fraction(leader, info, trade):
    """Share of the leader's position that a sale took (1.0 when the Data API cannot tell).

    A sale seen on the live stream is usually newer than the Data API position: then the size there is still
    the pre-sale one. Used by copy profiles and the autopilot alike.
    """
    sold = trade.get("size") or 0
    try:
        page = pm.positions_page(user=leader, cond=info["condition"], status="OPEN", limit=20)
    except (pm.UpstreamError, LookupError, ValueError):
        return 1.0
    rows = [r for r in page["rows"] if str(r.get("token_id")) == info["token"]]
    after = sum(r["current_size"] or 0 for r in rows)
    last = max((pm.num(r.get("last_event_at")) or 0 for r in rows), default=0)
    stale = bool(rows) and 0 < last < int(trade.get("timestamp") or 0)
    before = after if stale else after + sold
    return min(1.0, sold / before) if before > 0 else 1.0


def _copy_sell(db, uid, ct, trade, info):
    have = trading.available_shares(db, ct["account_id"], info["token"])
    if have <= 1e-6:
        return "skipped", "В профиле нет этой позиции", {}
    fraction = sell_fraction(ct["leader"], info, trade)
    shares = round(have * fraction, 6)
    if shares <= 1e-6:
        return "skipped", "Слишком маленький объём продажи", {}
    floor = None if ct["slippage"] is None else max(0.001, trade["price"] * (1 - ct["slippage"]))
    try:
        order = trading.place(db, uid, {"side": "sell", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "shares": shares, "max_price": floor},
                              account_id=ct["account_id"], source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = "Цена ушла дальше допустимого проскальзывания" if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"shares": shares}
    return "filled", "", {"amount": round(order["filled"] * (order["avg"] or 0) - order["fees"], 6),
                          "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


_locks_guard = threading.Lock()
_locks = {}


def _lock_for(cid):
    with _locks_guard:
        return _locks.setdefault(cid, threading.Lock())


def trade_key(r):
    """Identity of one leader fill, the same for the live stream and the Data API."""
    size = pm.num(r.get("size")) or 0.0
    return f"{str(r.get('transaction_hash') or '').lower()}:{r.get('token_id')}:{str(r.get('side') or '').upper()}:{round(size, 6):g}"


def handle(db, ct, trade, uid, *, infos=None, source="poll", detected=None):
    """Copy one leader fill unless this copy profile already did (from the stream or the Data API).

    Returns True when the fill was processed now. The check and the copy run under a per-profile lock,
    so the live stream and the polling fallback can never copy the same fill twice.
    """
    key = trade_key(trade)
    infos = {} if infos is None else infos
    with _lock_for(ct["id"]):
        if db.execute("SELECT 1 FROM copy_events WHERE copy_trade_id=? AND key_hash=? LIMIT 1",
                      (ct["id"], store.key_hash(key))).fetchone():
            return False
        tok, cond = str(trade.get("token_id") or ""), str(trade.get("condition_id") or "").lower()
        try:
            if tok not in infos:
                infos[tok] = trading.market_info(cond, tok)
            info = infos[tok]
            age = time.time() - (pm.num(trade.get("timestamp")) or time.time())
            if info["closed"] or not info["accepting"]:
                status, reason, extra = "skipped", "Рынок уже закрыт", {}
            elif (trade.get("side") or "").upper() == "BUY" and age > COPY_MAX_AGE:
                # The price may have gone anywhere since; a late sale is still mirrored (better to exit late than to stay in).
                ago = f"{int(age // 60)} мин" if age < 7200 else f"{int(age // 3600)} ч"
                status, reason, extra = "skipped", f"Сделка была {ago} назад — покупку копировать поздно", {}
            elif ct.get("autopilot"):
                from . import autopilot  # imported here: autopilot builds on this module
                side = (trade.get("side") or "").upper()
                status, reason, extra = (autopilot.copy_buy if side == "BUY" else autopilot.copy_sell)(db, uid, ct, trade, info)
            elif (trade.get("side") or "").upper() == "BUY":
                status, reason, extra = _copy_buy(db, uid, ct, trade, info)
            else:
                status, reason, extra = _copy_sell(db, uid, ct, trade, info)
        except (pm.UpstreamError, LookupError, ValueError) as exc:
            status, reason, extra = "failed", str(exc)[:200], {}
        _record(db, ct, status, reason, trade, source=source, detected=detected, **extra)
    return True


def poll(db, ct):
    """Copy the leader's fills that appeared in the Data API since the last check (fallback for the live stream)."""
    acc = db.execute("SELECT user_id, paused FROM accounts WHERE id=?", (ct["account_id"],)).fetchone()
    if not acc or acc["paused"] or ct["paused"]:
        return 0
    uid = acc["user_id"]
    page = pm.trades_page(user=ct["leader"], limit=50, taker_only=False, gap=0.2)
    seen = set(json.loads(ct["cursor_keys"] or "[]"))
    fresh = [r for r in page["rows"] if int(r["timestamp"] or 0) > ct["cursor_ts"]
             or (int(r["timestamp"] or 0) == ct["cursor_ts"] and _key(r) not in seen)]
    fresh.sort(key=lambda r: int(r["timestamp"] or 0))
    infos, done = {}, 0
    for trade in fresh:
        if handle(db, ct, trade, uid, infos=infos, source="poll", detected=time.time()):
            done += 1
        ts = int(trade["timestamp"] or 0)
        if ts > ct["cursor_ts"]:
            ct["cursor_ts"], seen = ts, set()
        seen.add(_key(trade))
        db.execute("UPDATE copy_trades SET cursor_ts=?, cursor_keys=? WHERE id=?", (ct["cursor_ts"], json.dumps(sorted(seen)), ct["id"]))
    db.execute("UPDATE copy_trades SET last_check=?, last_error='' WHERE id=?", (time.time(), ct["id"]))
    return done
