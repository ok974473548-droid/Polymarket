"""Autopilot: keeps a roster of auto-selected players, copies them from one shared budget and
replaces those who fall behind with the best player who passed probation on the bench.

States of a player for a user: bench (probation without money) → active (copied with money)
→ retired (cooldown) or banned. The rules are documented in AUTOPILOT.md.
"""

import json
import time
from datetime import datetime, timezone

from . import copytrade
from . import db as store
from . import notify
from . import polymarket as pm
from . import scout
from . import trading

DAY = 86400
REVIEW_EVERY = 600
EPS = 1e-6

SETTINGS = {
    # roster and money
    "active_count": 8, "bench_size": 4, "copy_pct": 10, "event_cap_pct": 10,
    # selection (same keys as scout.DEFAULTS)
    **scout.DEFAULTS,
    # what is copied
    "price_min": 0.02, "price_max": 0.97, "min_leader_trade": 5, "max_days_to_end": 60, "copy_slip": 0.03,
    # probation
    "probation_days": 14, "probation_min_bets": 5, "allow_early": True, "early_days": 7, "bootstrap": True,
    # replacement
    "loss_streak": 5, "recent_min_wr": 55, "max_drawdown_pct": 30, "inactive_days": 14,
    "rotation": True, "rotation_margin": 15, "min_tenure_days": 21, "max_swaps_per_day": 3, "cooldown_days": 30,
}
LIMITS = {
    "active_count": (int, 3, 20), "bench_size": (int, 0, 12), "copy_pct": (float, 2, 50), "event_cap_pct": (float, 2, 50),
    "min_bets": (int, 15, 300), "min_wr": (float, 50, 95), "min_lb": (float, 40, 90), "min_roi": (float, -10, 50),
    "min_edge": (float, 0, 0.2), "max_top_share": (float, 10, 100), "min_recent": (int, 0, 30), "max_slip": (float, 0.005, 0.1),
    "price_min": (float, 0.01, 0.5), "price_max": (float, 0.5, 0.99), "min_leader_trade": (float, 1, 1000),
    "max_days_to_end": (int, 0, 730), "copy_slip": (float, 0.005, 0.2),
    "probation_days": (int, 3, 60), "probation_min_bets": (int, 1, 50), "allow_early": (bool,), "early_days": (int, 1, 60),
    "bootstrap": (bool,), "loss_streak": (int, 3, 20), "recent_min_wr": (float, 20, 90), "max_drawdown_pct": (float, 5, 90),
    "inactive_days": (int, 3, 90), "rotation": (bool,), "rotation_margin": (float, 5, 50), "min_tenure_days": (int, 3, 120),
    "max_swaps_per_day": (int, 1, 10), "cooldown_days": (int, 0, 365),
}
LOG_KINDS = {"enable", "disable", "bench", "promote", "bootstrap", "pass", "fail", "replace", "rotate", "retire", "ban",
             "unban", "pin", "unpin", "settings", "release", "done", "manual"}


# ---------------------------------------------------------------- settings and account

def settings(db, uid):
    raw = json.loads(store.kv_get(db, f"ap:{uid}:settings", "{}") or "{}")
    out = {**SETTINGS, **{k: v for k, v in raw.items() if k in SETTINGS}}
    out["enabled"] = bool(raw.get("enabled"))
    return out


def _save_settings(db, uid, s):
    store.kv_set(db, f"ap:{uid}:settings", json.dumps(s))


def update_settings(db, uid, body):
    s = settings(db, uid)
    changed = []
    for key, value in (body or {}).items():
        if key not in LIMITS:
            continue
        spec = LIMITS[key]
        if spec[0] is bool:
            v = bool(value)
        else:
            v = pm.num(value)
            if v is None or not spec[1] <= v <= spec[2]:
                raise ValueError(f"Значение «{key}» должно быть от {spec[1]} до {spec[2]}")
            v = int(round(v)) if spec[0] is int else round(float(v), 4)
        if s.get(key) != v:
            s[key] = v
            changed.append(key)
    if s["price_min"] >= s["price_max"]:
        raise ValueError("Нижняя граница цены должна быть меньше верхней")
    if body and body.get("reset"):
        s = {**SETTINGS, "enabled": s["enabled"]}
        changed = ["reset"]
    _save_settings(db, uid, s)
    if changed:
        log(db, uid, "settings", "", "Настройки изменены" if changed != ["reset"] else "Настройки сброшены по умолчанию")
        store.kv_set(db, f"ap:{uid}:review_now", "1")
    return s


def account_id(db, uid):
    v = store.kv_get(db, f"ap:{uid}:account", "")
    if not v:
        return None
    row = db.execute("SELECT id FROM accounts WHERE id=? AND user_id=?", (int(v), uid)).fetchone()
    return row[0] if row else None


def ensure_account(db, uid):
    aid = account_id(db, uid)
    if aid:
        return aid
    with store.tx(db):
        cur = db.execute("INSERT INTO accounts(user_id, kind, name, balance, paused, created) VALUES(?,?,?,0,1,?)",
                         (uid, "copy", "Автопилот", time.time()))
    store.kv_set(db, f"ap:{uid}:account", str(cur.lastrowid))
    return cur.lastrowid


def enable(db, uid, on, budget=None):
    aid = ensure_account(db, uid)
    store.kv_set(db, "scout:wanted", "1")
    if budget:
        trading.transfer_from_main(db, uid, aid, budget)
    s = settings(db, uid)
    s["enabled"] = bool(on)
    _save_settings(db, uid, s)
    db.execute("UPDATE accounts SET paused=? WHERE id=?", (0 if on else 1, aid))
    log(db, uid, "enable" if on else "disable", "", "Автопилот включён" if on else "Автопилот выключен: новые копии не делаются")
    store.kv_set(db, f"ap:{uid}:review_now", "1")
    return overview(db, uid)


def log(db, uid, kind, wallet, text):
    db.execute("INSERT INTO ap_log(user_id, at, kind, wallet, text) VALUES(?,?,?,?,?)", (uid, time.time(), kind, wallet or "", text))


def _who(db, wallet):
    row = db.execute("SELECT name FROM scout_wallets WHERE wallet=?", (wallet,)).fetchone()
    return (row[0] if row and row[0] else wallet[:6] + "…" + wallet[-4:])


# ---------------------------------------------------------------- copying (called by copytrade.poll)

def _equity(db, aid):
    return trading.equity(db, aid)


def _open_cost(db, uid, *, wallet=None, event_slug=None):
    sql = "SELECT COALESCE(SUM(CASE WHEN bought>0 THEN cost*shares/bought ELSE 0 END),0) FROM ap_bets WHERE user_id=? AND status='open'"
    params = [uid]
    if wallet:
        sql += " AND wallet=?"
        params.append(wallet)
    if event_slug:
        sql += " AND event_slug=?"
        params.append(event_slug)
    return db.execute(sql, params).fetchone()[0]


def copy_buy(db, uid, ct, trade, info):
    """Size and place an autopilot copy of a leader's buy. Returns (status, reason, extra) like copytrade."""
    if ct.get("mode") != "normal":
        return "skipped", "Игрок выведен из состава: повторяются только его продажи", {}
    s = settings(db, uid)
    price, size = trade.get("price"), trade.get("size")
    if price is None or size is None:
        return "skipped", "Сделка игрока без цены", {}
    if price * size < s["min_leader_trade"]:
        return "skipped", f"Сделка игрока меньше ${s['min_leader_trade']:g}", {}
    if not s["price_min"] - EPS <= price <= s["price_max"] + EPS:
        return "skipped", f"Цена ${price:.2f} вне диапазона ${s['price_min']:.2f}–{s['price_max']:.2f}", {}
    if scout.SHORT_TERM.search(str(trade.get("title") or "")):
        return "skipped", "Краткосрочный крипторынок: задержка копии слишком велика", {}
    end = scout._date_ts(info.get("end"))
    if s["max_days_to_end"] and end and end - time.time() > s["max_days_to_end"] * DAY:
        return "skipped", f"До разрешения {int((end - time.time()) / DAY)} дн., лимит {s['max_days_to_end']}", {}
    aid = ct["account_id"]
    eq = _equity(db, aid)
    share = eq["total"] / s["active_count"]
    amount = share * s["copy_pct"] / 100
    room = share - _open_cost(db, uid, wallet=ct["leader"])
    if room < copytrade.MIN_COPY:
        return "skipped", "Доля игрока занята открытыми ставками", {}
    amount = min(amount, room)
    event_room = eq["total"] * s["event_cap_pct"] / 100 - _open_cost(db, uid, event_slug=info.get("event_slug") or "")
    if event_room < copytrade.MIN_COPY:
        return "skipped", "Достигнут лимит автопилота на одно событие", {}
    amount = min(amount, event_room)
    cash = db.execute("SELECT balance FROM accounts WHERE id=?", (aid,)).fetchone()[0]
    amount = round(min(amount, cash), 2)
    if amount < copytrade.MIN_COPY:
        return "skipped", "Недостаточно свободных денег в автопилоте" if cash < copytrade.MIN_COPY else "Размер копии меньше $1", {}
    cap = min(0.999, price + s["copy_slip"])
    try:
        order = trading.place(db, uid, {"side": "buy", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "amount": amount, "max_price": cap},
                              account_id=aid, source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = f"Цена ушла больше чем на ${s['copy_slip']:.2f}" if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"amount": amount}
    spent = order["filled"] * (order["avg"] or 0) + order["fees"]
    _record_buy(db, uid, aid, ct, info, trade, order["filled"], spent)
    return "filled", "", {"amount": round(spent, 6), "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


def _record_buy(db, uid, aid, ct, info, trade, shares, spent):
    if shares <= EPS:
        return
    row = db.execute("SELECT id FROM ap_bets WHERE user_id=? AND wallet=? AND token_id=? AND status='open'",
                     (uid, ct["leader"], info["token"])).fetchone()
    if row:
        db.execute("UPDATE ap_bets SET shares=shares+?, bought=bought+?, cost=cost+? WHERE id=?", (shares, shares, spent, row[0]))
    else:
        db.execute("""INSERT INTO ap_bets(user_id, account_id, wallet, copy_trade_id, token_id, condition_id, title, outcome, event_slug,
                      image, opened, shares, bought, cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (uid, aid, ct["leader"], ct["id"], info["token"], info["condition"],
                    info.get("question") or trade.get("title") or "", info.get("outcome") or trade.get("outcome") or "",
                    info.get("event_slug") or trade.get("event_slug") or "", info.get("event_image") or trade.get("icon") or "",
                    time.time(), shares, shares, spent))


def _close_part(db, bet, shares, proceeds):
    left = max(0.0, bet["shares"] - shares)
    if left <= EPS:
        total = bet["proceeds"] + proceeds
        db.execute("UPDATE ap_bets SET shares=0, proceeds=?, status='closed', closed=?, pnl=? WHERE id=?",
                   (total, time.time(), total - bet["cost"], bet["id"]))
    else:
        db.execute("UPDATE ap_bets SET shares=?, proceeds=proceeds+? WHERE id=?", (left, proceeds, bet["id"]))


def copy_sell(db, uid, ct, trade, info):
    """Mirror a leader's sale within the shares this player's copies hold."""
    bet = store.one(db.execute("SELECT * FROM ap_bets WHERE user_id=? AND wallet=? AND token_id=? AND status='open'",
                               (uid, ct["leader"], info["token"])))
    if not bet:
        return "skipped", "У автопилота нет копии этой позиции", {}
    sold = trade.get("size") or 0
    fraction = 1.0
    try:
        page = pm.positions_page(user=ct["leader"], cond=info["condition"], status="OPEN", limit=20)
        after = sum(r["current_size"] or 0 for r in page["rows"] if str(r.get("token_id")) == info["token"])
        fraction = min(1.0, sold / (after + sold)) if after + sold > 0 else 1.0
    except (pm.UpstreamError, LookupError, ValueError):
        pass
    have = trading.available_shares(db, ct["account_id"], info["token"])
    shares = round(min(have, bet["shares"] * fraction if fraction < 0.999 else bet["shares"]), 6)
    if shares <= EPS:
        return "skipped", "Слишком маленький объём продажи", {}
    s = settings(db, uid)
    floor = max(0.001, (trade.get("price") or 0) - s["copy_slip"])
    try:
        order = trading.place(db, uid, {"side": "sell", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "shares": shares, "max_price": floor},
                              account_id=ct["account_id"], source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = f"Цена ушла больше чем на ${s['copy_slip']:.2f}" if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"shares": shares}
    got = order["filled"] * (order["avg"] or 0) - order["fees"]
    _close_part(db, bet, order["filled"], got)
    return "filled", "", {"amount": round(got, 6), "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


def sync_bets(db, uid):
    """Close copies of resolved markets and of positions the user sold by hand."""
    aid = account_id(db, uid)
    if not aid:
        return
    open_bets = store.rows(db.execute("SELECT * FROM ap_bets WHERE user_id=? AND status='open'", (uid,)))
    by_token = {}
    for b in open_bets:
        by_token.setdefault(b["token_id"], []).append(b)
    for tok, bets in by_token.items():
        pos = store.one(db.execute("SELECT * FROM positions WHERE account_id=? AND token_id=?", (aid, tok)))
        if not pos:
            continue
        with store.tx(db):
            if pos["status"] == "redeemed":
                per_share = pos["last_price"] or 0.0
                for b in bets:
                    _close_part(db, b, b["shares"], b["shares"] * per_share)
                continue
            held = sum(b["shares"] for b in bets)
            if pos["shares"] < held - EPS:
                ratio = 1 - max(0.0, pos["shares"]) / held
                for b in bets:
                    _close_part(db, b, b["shares"] * ratio, b["shares"] * ratio * (pos["last_price"] or 0.0))


# ---------------------------------------------------------------- player statistics

def _copies(db, uid, wallet, since=None):
    sql = "SELECT * FROM ap_bets WHERE user_id=? AND wallet=?"
    params = [uid, wallet]
    if since:
        sql += " AND opened>=?"
        params.append(since)
    return store.rows(db.execute(sql + " ORDER BY opened", params))


def copy_stats(db, uid, wallet, since=None):
    bets = _copies(db, uid, wallet, since)
    aid = account_id(db, uid)
    closed = sorted([b for b in bets if b["status"] == "closed" and abs(b["pnl"] or 0) > 0.002 * max(b["cost"], 1)],
                    key=lambda b: b["closed"])
    wins = sum(1 for b in closed if b["pnl"] > 0)
    streak = 0
    for b in reversed(closed):
        w = b["pnl"] > 0
        if streak == 0:
            streak = 1 if w else -1
        elif (streak > 0) == w:
            streak += 1 if w else -1
        else:
            break
    last = closed[-20:]
    open_cost = open_value = 0.0
    for b in bets:
        if b["status"] != "open":
            continue
        open_cost += b["cost"] * b["shares"] / b["bought"] if b["bought"] else 0
        price = db.execute("SELECT last_price FROM positions WHERE account_id=? AND token_id=?", (aid, b["token_id"])).fetchone()
        open_value += b["shares"] * (price[0] if price and price[0] is not None else 0)
    realized = sum(b["pnl"] or 0 for b in bets if b["status"] == "closed")
    return {"closed": len(closed), "wins": wins, "losses": len(closed) - wins, "wr": wins / len(closed) if closed else None,
            "streak": streak, "recent_n": len(last), "recent_wr": (sum(1 for b in last if b["pnl"] > 0) / len(last)) if last else None,
            "open": sum(1 for b in bets if b["status"] == "open"), "open_cost": open_cost, "open_value": open_value,
            "realized": realized, "pnl": realized + open_value - open_cost,
            "last_copy": max((b["opened"] for b in bets), default=None)}


def shadow_stats(db, wallet, since):
    """Probation: the player's new bets since `since`, valued as copies (no money involved)."""
    cand = scout.candidate(db, wallet)
    slip = (cand["metrics"] or {}).get("slip") if cand and cand["metrics"] else scout.DEFAULT_SLIP
    decided, open_r, new = [], [], 0
    for b in scout.stored_bets(db, wallet):
        if b["opened"] < since or b["short"] or not scout.PRICE_MIN <= b["entry"] <= scout.PRICE_MAX:
            continue
        if b["entry"] * b["size"] < scout.MIN_COST:
            continue
        new += 1
        if b["state"] == "open":
            if b["mark"] is not None:
                open_r.append(scout.copy_result(b["entry"], b["mark"], slip or scout.DEFAULT_SLIP, False)[0])
        else:
            r, _ = scout.copy_result(b["entry"], b["exit"], slip or scout.DEFAULT_SLIP, b["state"] == "resolved")
            if abs(r) >= scout.PUSH:
                decided.append(r)
    wins = sum(1 for r in decided if r > 0)
    every = decided + open_r
    return {"new": new, "decided": len(decided), "wins": wins, "wr": wins / len(decided) if decided else None,
            "roi": sum(decided) / len(decided) if decided else None, "open": len(open_r),
            "roi_all": sum(every) / len(every) if every else None}


def _relaxed(s):
    """Thresholds with a margin for players already selected, so they are not dropped on noise."""
    return {**s, "min_lb": s["min_lb"] - 5, "min_wr": s["min_wr"] - 7, "min_roi": s["min_roi"] - 2, "min_recent": 0,
            "min_edge": max(0.0, s["min_edge"] - 0.01), "max_slip": s["max_slip"] + 0.01}


def _verdict(db, wallet, s, relaxed=False):
    cand = scout.candidate(db, wallet)
    if not cand or not cand["metrics"]:
        return None, None
    return cand, scout.judge(cand["metrics"], cand["kind"], _relaxed(s) if relaxed else s)


def _probation(db, player, s, now):
    shadow = shadow_stats(db, player["wallet"], player["bench_since"])
    days = (now - player["bench_since"]) / DAY
    cand, verdict = _verdict(db, player["wallet"], s, relaxed=True)
    target = s["min_wr"] / 100
    fail = None
    if verdict is not None and not verdict["passed"] and cand["profiled"] and cand["profiled"] > player["bench_since"]:
        fail = "Перестал проходить отбор: " + ", ".join(verdict["failed"]).lower()
    elif shadow["decided"] >= 5 and shadow["wr"] < target - 0.25:
        fail = f"На испытании выиграно {shadow['wr'] * 100:.0f}% из {shadow['decided']} ставок"
    elif shadow["roi_all"] is not None and shadow["roi_all"] < -0.25:
        fail = f"Результат теневых копий {shadow['roi_all'] * 100:.0f}%"
    elif days >= 2 * s["probation_days"] and shadow["new"] < 2:
        fail = "За время испытания почти нет новых ставок"
    healthy = fail is None and (shadow["roi_all"] is None or shadow["roi_all"] >= -0.05) and \
        (shadow["decided"] < 3 or shadow["wr"] >= target - 0.15)
    ready = healthy and days >= s["probation_days"] and shadow["new"] >= s["probation_min_bets"]
    early = healthy and days >= s["early_days"]
    return {"days": days, "need_days": s["probation_days"], "need_bets": s["probation_min_bets"], "ready": ready,
            "early_ok": early, "fail": fail, "shadow": shadow}


def _active_problem(db, uid, player, s, share, now):
    st = copy_stats(db, uid, player["wallet"], player["active_since"])
    if st["closed"] >= 5:
        if st["streak"] <= -s["loss_streak"]:
            return f"Серия из {-st['streak']} проигрышей подряд", st
        if st["recent_n"] >= 10 and st["recent_wr"] * 100 < s["recent_min_wr"]:
            return f"Выиграно {st['recent_wr'] * 100:.0f}% из последних {st['recent_n']} копий", st
        if share > 0 and st["pnl"] <= -share * s["max_drawdown_pct"] / 100:
            return f"Убыток копий ${-st['pnl']:,.2f} — {(-st['pnl'] / share) * 100:.0f}% доли игрока", st
    cand = scout.candidate(db, player["wallet"])
    last_bet = db.execute("SELECT MAX(opened) FROM scout_bets WHERE wallet=?", (player["wallet"],)).fetchone()[0]
    last = max(x for x in (st["last_copy"], last_bet, player["active_since"]) if x)
    if now - last >= s["inactive_days"] * DAY:
        return f"Нет новых ставок {int((now - last) / DAY)} дн.", st
    if cand and cand["metrics"] and cand["profiled"] and cand["profiled"] > player["active_since"]:
        verdict = scout.judge(cand["metrics"], cand["kind"], _relaxed(s))
        if not verdict["passed"]:
            return "Больше не проходит отбор: " + ", ".join(verdict["failed"]).lower(), st
    return None, st


# ---------------------------------------------------------------- roster moves

def _swaps_today(db, uid):
    return db.execute("SELECT COUNT(*) FROM ap_log WHERE user_id=? AND kind IN ('replace','rotate') AND at>?",
                      (uid, time.time() - DAY)).fetchone()[0]


def _start_copy(db, uid, aid, wallet):
    ts, keys = copytrade._latest_cursor(wallet)
    with store.tx(db):
        cur = db.execute("""INSERT INTO copy_trades(account_id, leader, nickname, sizing, amount, notify, cursor_ts, cursor_keys,
                            created, autopilot, mode) VALUES(?,?,?,?,?,0,?,?,?,1,'normal')""",
                         (aid, wallet, _who(db, wallet), "fixed", 1.1, ts, json.dumps(keys), time.time()))
    return cur.lastrowid


def _stop_copy(db, player):
    if player.get("copy_trade_id"):
        db.execute("UPDATE copy_trades SET mode='exit' WHERE id=? AND mode='normal'", (player["copy_trade_id"],))


def _promote(db, uid, aid, wallet, kind, text, early=False):
    ctid = _start_copy(db, uid, aid, wallet)
    now = time.time()
    db.execute("""INSERT INTO ap_players(user_id, wallet, state, since, active_since, early, copy_trade_id, reason)
                  VALUES(?,?,'active',?,?,?,?, '') ON CONFLICT(user_id, wallet) DO UPDATE SET state='active', active_since=?,
                  early=?, copy_trade_id=?, reason=''""",
               (uid, wallet, now, now, int(early), ctid, now, int(early), ctid))
    log(db, uid, kind, wallet, text)


def _retire(db, uid, player, kind, reason, s, cooldown=True):
    _stop_copy(db, player)
    until = time.time() + (s["cooldown_days"] * DAY if cooldown else 0)
    db.execute("UPDATE ap_players SET state='retired', reason=?, until=?, pinned=0 WHERE user_id=? AND wallet=?",
               (reason, until, uid, player["wallet"]))
    verb = {"replace": "заменён", "rotate": "выведен из состава", "fail": "не прошёл испытание", "retire": "выбыл"}.get(kind, "выбыл")
    log(db, uid, kind, player["wallet"], f"{_who(db, player['wallet'])} {verb}: {reason[:1].lower() + reason[1:]}")


def _players(db, uid, state=None):
    sql, params = "SELECT * FROM ap_players WHERE user_id=?", [uid]
    if state:
        sql += " AND state=?"
        params.append(state)
    return store.rows(db.execute(sql, params))


def _pool(db, uid, s):
    """Passing candidates that are not in this user's roster in any state."""
    taken = {r["wallet"] for r in _players(db, uid)}
    return [c for c in scout.candidates(db, s) if c["passed"] and c["wallet"] not in taken]


def _finish_exits(db, uid, aid):
    """Exit-only copies with nothing left to sell are finished."""
    for ct in store.rows(db.execute("SELECT id FROM copy_trades WHERE account_id=? AND autopilot=1 AND mode='exit'", (aid,))):
        if not db.execute("SELECT 1 FROM ap_bets WHERE user_id=? AND copy_trade_id=? AND status='open'", (uid, ct["id"])).fetchone():
            db.execute("UPDATE copy_trades SET mode='done', paused=1 WHERE id=?", (ct["id"],))


def review(db, uid, now=None):
    """One autopilot cycle for a user: probation, problems, replacements, refills."""
    now = now or time.time()
    s = settings(db, uid)
    aid = account_id(db, uid)
    if not aid:
        return
    sync_bets(db, uid)
    for p in _players(db, uid, "retired"):
        if p["until"] and p["until"] < now:
            db.execute("DELETE FROM ap_players WHERE user_id=? AND wallet=?", (uid, p["wallet"]))
    if not s["enabled"]:
        _finish_exits(db, uid, aid)
        store.kv_set(db, f"ap:{uid}:reviewed", str(now))
        return
    eq = _equity(db, aid)
    share = eq["total"] / s["active_count"]
    swaps_left = s["max_swaps_per_day"] - _swaps_today(db, uid)
    vacancies = 0

    # 1. active players that fall behind
    for p in sorted(_players(db, uid, "active"), key=lambda r: r["active_since"] or 0):
        problem, _ = _active_problem(db, uid, p, s, share, now)
        db.execute("UPDATE ap_players SET checked=?, reason=? WHERE user_id=? AND wallet=?",
                   (now, problem or "", uid, p["wallet"]))
        if problem and not p["pinned"] and swaps_left > 0:
            _retire(db, uid, p, "replace", problem, s)
            notify.add(db, uid, "autopilot", "Автопилот заменил игрока", f"{_who(db, p['wallet'])}: {problem}", "/autopilot?tab=log")
            swaps_left -= 1
            vacancies += 1

    # 2. bench probation
    probation = {}
    for p in _players(db, uid, "bench"):
        pr = _probation(db, p, s, now)
        probation[p["wallet"]] = pr
        db.execute("UPDATE ap_players SET shadow=?, checked=? WHERE user_id=? AND wallet=?",
                   (json.dumps({k: v for k, v in pr.items() if k != "fail"}), now, uid, p["wallet"]))
        if pr["fail"] and not p["pinned"]:
            _retire(db, uid, p, "fail", pr["fail"], s)
            probation.pop(p["wallet"])
        elif pr["ready"] and not json.loads(p["shadow"] or "{}").get("ready"):
            log(db, uid, "pass", p["wallet"], f"{_who(db, p['wallet'])} прошёл испытание и готов войти в состав")

    def bench_by_score(only_ready):
        out = []
        for p in _players(db, uid, "bench"):
            pr = probation.get(p["wallet"])
            if not pr or not (pr["ready"] or (not only_ready and pr["early_ok"])):
                continue
            cand = scout.candidate(db, p["wallet"])
            out.append((scout.score(cand["metrics"]) if cand and cand["metrics"] else 0, p, pr))
        return sorted(out, key=lambda x: -x[0])

    # 3. fill free places
    active = _players(db, uid, "active")
    if len(active) > s["active_count"]:
        extra = sorted([p for p in active if not p["pinned"]],
                       key=lambda p: (scout.candidate(db, p["wallet"]) or {}).get("score") or 0)[:len(active) - s["active_count"]]
        for p in extra:
            _retire(db, uid, p, "retire", "Размер состава уменьшен", s, cooldown=False)
        active = _players(db, uid, "active")
    free = s["active_count"] - len(active)
    # Until the roster has been full once, places go straight to players who pass selection:
    # suitable players are rare and are found gradually while the pool is being checked.
    first_time = not store.kv_get(db, f"ap:{uid}:bootstrapped", "")
    if free > 0 and first_time and s["bootstrap"]:
        picks = _pool(db, uid, s)[:free]
        for c in picks:
            _promote(db, uid, aid, c["wallet"], "bootstrap",
                     f"{c['name'] or c['wallet'][:10]} — в стартовом составе по истории (балл {c['score']:.0f})")
        free -= len(picks)
    if free > 0:
        ready = bench_by_score(only_ready=True)
        if len(ready) < free and s["allow_early"]:
            ready += [x for x in bench_by_score(only_ready=False) if x[1]["wallet"] not in {r[1]["wallet"] for r in ready}]
        for sc, p, pr in ready[:free]:
            early = not pr["ready"]
            _promote(db, uid, aid, p["wallet"], "promote",
                     f"{_who(db, p['wallet'])} вошёл в состав" + (" досрочно: готовых на скамейке не было" if early else
                                                                  f" после испытания ({pr['shadow']['new']} ставок за {pr['days']:.0f} дн.)"),
                     early=early)

    # 4. rotation: a clearly stronger ready player replaces the weakest settled one
    if s["rotation"] and swaps_left > 0:
        ready = bench_by_score(only_ready=True)
        settled = []
        for p in _players(db, uid, "active"):
            if p["pinned"] or now - (p["active_since"] or now) < s["min_tenure_days"] * DAY:
                continue
            cand = scout.candidate(db, p["wallet"])
            settled.append((scout.score(cand["metrics"]) if cand and cand["metrics"] else 0, p))
        settled.sort(key=lambda x: x[0])
        if ready and settled and ready[0][0] >= settled[0][0] + s["rotation_margin"]:
            weak_score, weak = settled[0]
            best_score, best, _ = ready[0]
            _retire(db, uid, weak, "rotate", f"Ротация: на скамейке игрок сильнее на {best_score - weak_score:.0f} баллов", s, cooldown=False)
            _promote(db, uid, aid, best["wallet"], "promote", f"{_who(db, best['wallet'])} вошёл в состав вместо {_who(db, weak['wallet'])}")
            notify.add(db, uid, "autopilot", "Автопилот усилил состав",
                       f"{_who(db, best['wallet'])} вместо {_who(db, weak['wallet'])}", "/autopilot?tab=log")

    # 5. keep the bench full
    bench = _players(db, uid, "bench")
    need = s["bench_size"] - len(bench)
    if need > 0:
        for c in _pool(db, uid, s)[:need]:
            db.execute("INSERT INTO ap_players(user_id, wallet, state, since, bench_since) VALUES(?,?,'bench',?,?)",
                       (uid, c["wallet"], now, now))
            log(db, uid, "bench", c["wallet"], f"{c['name'] or c['wallet'][:10]} на скамейке: начато испытание (балл {c['score']:.0f})")
    _finish_exits(db, uid, aid)
    if len(_players(db, uid, "active")) >= s["active_count"]:
        store.kv_set(db, f"ap:{uid}:bootstrapped", "1")
    store.kv_set(db, f"ap:{uid}:reviewed", str(now))
    store.kv_set(db, f"ap:{uid}:review_now", "")


# ---------------------------------------------------------------- manual actions

def action(db, uid, wallet, what):
    wallet = pm.address(wallet).lower()
    s = settings(db, uid)
    p = store.one(db.execute("SELECT * FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet)))
    who = _who(db, wallet)
    if what == "bench":
        scout.add_wallet(db, wallet)
        store.kv_set(db, "scout:wanted", "1")
        if p and p["state"] in ("active", "bench"):
            raise ValueError("Игрок уже в автопилоте")
        now = time.time()
        db.execute("""INSERT INTO ap_players(user_id, wallet, state, since, bench_since, manual) VALUES(?,?,'bench',?,?,1)
                      ON CONFLICT(user_id, wallet) DO UPDATE SET state='bench', bench_since=?, manual=1, reason='', until=NULL""",
                   (uid, wallet, now, now, now))
        log(db, uid, "manual", wallet, f"{who} добавлен на скамейку вручную")
    elif not p:
        raise LookupError("Игрока нет в автопилоте")
    elif what in ("pin", "unpin"):
        db.execute("UPDATE ap_players SET pinned=? WHERE user_id=? AND wallet=?", (int(what == "pin"), uid, wallet))
        log(db, uid, what, wallet, f"{who} {'закреплён: автопилот не будет его менять' if what == 'pin' else 'откреплён'}")
    elif what == "replace":
        if p["state"] != "active":
            raise ValueError("Заменить можно только игрока из состава")
        _retire(db, uid, p, "retire", "Заменён вручную", s)
    elif what == "promote":
        if p["state"] != "bench":
            raise ValueError("В состав переводится игрок со скамейки")
        active = len(_players(db, uid, "active"))
        if active >= s["active_count"]:
            raise ValueError("Состав заполнен: сначала замените кого-то или увеличьте размер состава")
        aid = ensure_account(db, uid)
        _promote(db, uid, aid, wallet, "promote", f"{who} переведён в состав вручную", early=True)
    elif what == "ban":
        if p["state"] == "active":
            _stop_copy(db, p)
        db.execute("UPDATE ap_players SET state='banned', pinned=0, reason='Исключён вручную', until=NULL WHERE user_id=? AND wallet=?",
                   (uid, wallet))
        log(db, uid, "ban", wallet, f"{who} исключён: автопилот больше не будет его выбирать")
    elif what in ("unban", "remove"):
        if p["state"] == "active":
            raise ValueError("Игрок в составе: сначала замените его")
        db.execute("DELETE FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet))
        log(db, uid, "unban" if what == "unban" else "retire", wallet,
            f"{who} {'снова может быть выбран' if what == 'unban' else 'убран со скамейки'}")
    else:
        raise ValueError("Неизвестное действие")
    store.kv_set(db, f"ap:{uid}:review_now", "1")
    return player(db, uid, wallet)


# ---------------------------------------------------------------- views

def _card(db, uid, p, s, share, now):
    cand = scout.candidate(db, p["wallet"])
    m = (cand or {}).get("metrics") or {}
    verdict = scout.judge(m, cand["kind"], s) if m else None
    card = {"wallet": p["wallet"], "name": (cand or {}).get("name") or "", "image": (cand or {}).get("image") or "",
            "state": p["state"], "pinned": bool(p["pinned"]), "manual": bool(p["manual"]), "early": bool(p["early"]),
            "since": p["since"], "bench_since": p["bench_since"], "active_since": p["active_since"], "reason": p["reason"],
            "until": p["until"], "kind": (cand or {}).get("kind"), "kind_label": scout.TYPES.get((cand or {}).get("kind"), "Считается"),
            "profiled": (cand or {}).get("profiled"),
            "profile": {k: m.get(k) for k in ("n", "wr", "lb", "roi", "edge", "avg_entry", "new_14d", "slip_measured", "recent_wr")} if m else None,
            "score": verdict["score"] if verdict else None, "passed": verdict["passed"] if verdict else None,
            "failed": verdict["failed"] if verdict else []}
    if p["state"] in ("active", "retired"):
        st = copy_stats(db, uid, p["wallet"], p["active_since"])
        card["copies"] = st
        card["share"] = share
        card["used"] = st["open_cost"]
        warn = []
        if st["streak"] <= -2:
            warn.append(f"{-st['streak']} проигрыша подряд")
        if p["reason"] and p["state"] == "active":
            warn.append(p["reason"] + (" — игрок закреплён" if p["pinned"] else ""))
        card["warnings"] = warn
    if p["state"] == "bench":
        card["probation"] = json.loads(p["shadow"]) if p["shadow"] else None
    return card


def overview(db, uid):
    s = settings(db, uid)
    aid = account_id(db, uid)
    now = time.time()
    acc = trading.summary(db, uid, aid) if aid else None
    share = (acc["total"] / s["active_count"]) if acc else 0
    players = _players(db, uid)
    cards = {k: [] for k in ("active", "bench", "retired", "banned")}
    for p in players:
        cards[p["state"]].append(_card(db, uid, p, s, share, now))
    cards["active"].sort(key=lambda c: -(c["score"] or 0))
    cards["bench"].sort(key=lambda c: (not (c.get("probation") or {}).get("ready"), -(c["score"] or 0)))
    cards["retired"].sort(key=lambda c: -(c["until"] or 0))
    bets = store.rows(db.execute("SELECT status, pnl, cost FROM ap_bets WHERE user_id=?", (uid,)))
    closed = [b for b in bets if b["status"] == "closed" and abs(b["pnl"] or 0) > 0.002 * max(b["cost"], 1)]
    wins = sum(1 for b in closed if b["pnl"] > 0)
    expected = [c["profile"]["wr"] for c in cards["active"] if c.get("profile") and c["profile"].get("wr") is not None]
    return {"settings": s, "defaults": SETTINGS, "enabled": s["enabled"], "account": acc, "share": share,
            "stats": {"closed": len(closed), "wins": wins, "wr": wins / len(closed) if closed else None,
                      "realized": sum(b["pnl"] or 0 for b in closed),
                      "open": sum(1 for b in bets if b["status"] == "open"),
                      "expected_wr": sum(expected) / len(expected) if expected else None},
            "players": cards, "swaps_today": _swaps_today(db, uid),
            "reviewed": float(store.kv_get(db, f"ap:{uid}:reviewed", "0") or 0) or None, "scout": scout.status(db)}


def player(db, uid, wallet):
    wallet = pm.address(wallet).lower()
    s = settings(db, uid)
    cand = scout.candidate(db, wallet)
    p = store.one(db.execute("SELECT * FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet)))
    aid = account_id(db, uid)
    share = (trading.equity(db, aid)["total"] / s["active_count"]) if aid else 0
    out = {"wallet": wallet, "in_roster": bool(p), "card": _card(db, uid, p, s, share, time.time()) if p else None,
           "name": (cand or {}).get("name") or "", "image": (cand or {}).get("image") or "", "profiled": (cand or {}).get("profiled"),
           "kind": (cand or {}).get("kind"), "kind_label": scout.TYPES.get((cand or {}).get("kind"), ""),
           "facts": (cand or {}).get("facts"), "sources": (cand or {}).get("sources") or [], "metrics": None, "verdict": None}
    if cand and cand["metrics"]:
        out["metrics"] = cand["metrics"]
        out["verdict"] = scout.judge(cand["metrics"], cand["kind"], s)
    recent = store.rows(db.execute("""SELECT title, outcome, event_slug, entry, exit, state, mark, opened, closed, size FROM scout_bets
                                      WHERE wallet=? ORDER BY opened DESC LIMIT 25""", (wallet,)))
    out["recent_bets"] = recent
    out["copies"] = _copies(db, uid, wallet)[-50:][::-1] if p else []
    return out


def bets(db, uid, status="", wallet=""):
    sql, params = "SELECT b.*, s.name AS player FROM ap_bets b LEFT JOIN scout_wallets s ON s.wallet=b.wallet WHERE b.user_id=?", [uid]
    if status in ("open", "closed"):
        sql += " AND b.status=?"
        params.append(status)
    if wallet:
        sql += " AND b.wallet=?"
        params.append(wallet.lower())
    rows = store.rows(db.execute(sql + " ORDER BY b.opened DESC LIMIT 300", params))
    aid = account_id(db, uid)
    for r in rows:
        price = db.execute("SELECT last_price FROM positions WHERE account_id=? AND token_id=?", (aid, r["token_id"])).fetchone()
        r["price"] = price[0] if price else None
        r["entry"] = r["cost"] / r["bought"] if r["bought"] else None
        r["value"] = r["shares"] * r["price"] if r["status"] == "open" and r["price"] is not None else None
    return rows


def log_rows(db, uid, limit=300):
    rows = store.rows(db.execute("""SELECT l.*, s.name, s.image FROM ap_log l LEFT JOIN scout_wallets s ON s.wallet=l.wallet
                                    WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?""", (uid, limit)))
    return rows


def backtest_result(db, uid):
    raw = store.kv_get(db, f"ap:{uid}:backtest", "")
    return json.loads(raw) if raw else None


def run_backtest(db, uid):
    s = settings(db, uid)
    res = scout.backtest(db, s, s["active_count"])
    store.kv_set(db, f"ap:{uid}:backtest", json.dumps(res))
    store.kv_set(db, f"ap:{uid}:backtest_req", "")
    return res


def users(db):
    """Users who have an autopilot account."""
    return [int(r[0].split(":")[1]) for r in db.execute("SELECT key FROM kv WHERE key LIKE 'ap:%:account'")]


def is_autopilot_account(db, aid):
    return db.execute("SELECT 1 FROM kv WHERE key LIKE 'ap:%:account' AND value=?", (str(aid),)).fetchone() is not None


def day_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
