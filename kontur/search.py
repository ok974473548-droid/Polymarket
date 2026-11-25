"""Smart search: a local full-text index of events, traders and tags, ranked by relevance, popularity
and what the user cares about; filters from kontur.query; the user's own data; trades; saved searches
with alerts; recent queries and opened items.
"""

import difflib
import json
import math
import re
import time
from datetime import datetime

from . import categories
from . import db as store
from . import notify
from . import polymarket as pm
from . import query as qparse
from . import traders

DAY = 86400
EVENT_PAGE = 50
EVENT_PAGES = 30          # top 1500 events by 24h volume
NEW_PAGES = 2             # plus the 100 newest
STALE = 3 * DAY
_vocab = {"at": 0, "terms": set(), "by_first": {}}


# ---------------------------------------------------------------- index

def _upsert(db, kind, key, title, body, image, pop, data, keep_body=False):
    row = db.execute("SELECT id FROM search_items WHERE kind=? AND key=?", (kind, key)).fetchone()
    now = time.time()
    if row:
        rid = row[0]
        if keep_body:
            db.execute("UPDATE search_items SET updated=? WHERE id=?", (now, rid))
            return rid
        db.execute("UPDATE search_items SET title=?, image=?, pop=?, data=?, updated=? WHERE id=?",
                   (title, image, pop, json.dumps(data), now, rid))
        db.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
    else:
        rid = db.execute("INSERT INTO search_items(kind, key, title, image, pop, data, updated) VALUES(?,?,?,?,?,?,?)",
                         (kind, key, title, image, pop, json.dumps(data), now)).lastrowid
    db.execute("INSERT INTO search_fts(rowid, title, body) VALUES(?,?,?)", (rid, title, body))
    return rid


def _ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _top(e):
    """Leading outcomes: candidates of a multi-market event, or Yes/No of a single market."""
    contracts = [c for c in e.get("contracts") or [] if not c.get("closed") and c.get("outcomes")]
    if len(contracts) > 1:
        rows = [{"name": c.get("label") or c.get("question"), "price": c["outcomes"][0]["price"],
                 "change": c.get("change24h")} for c in contracts]
        rows.sort(key=lambda r: -(r["price"] or 0))
        return rows[:3]
    if contracts:
        c = contracts[0]
        return [{"name": o["name"], "price": o["price"], "change": c.get("change24h") if i == 0 else None}
                for i, o in enumerate(c["outcomes"][:2])]
    return []


def index_event(db, e, keep_body=False):
    labels = []
    for c in e.get("contracts") or []:
        labels += [c.get("label") or "", c.get("question") or ""]
    tags = [t["slug"] for t in e.get("tags") or []]
    body = " ".join([*labels[:80], *(t["label"] for t in e.get("tags") or []), e["slug"].replace("-", " ")])
    data = {"slug": e["slug"], "id": e.get("id"), "tags": tags, "tag_labels": [t["label"] for t in e.get("tags") or []][:6],
            "end": _ts(e.get("end")), "created": _ts(e.get("start") or e.get("created")), "volume": e.get("volume"),
            "volume24h": e.get("volume24h"), "liquidity": e.get("liquidity"), "oi": e.get("oi"), "count": e.get("count"),
            "top": _top(e),
            "prices": sorted({round(c["outcomes"][0]["price"], 4) for c in e.get("contracts") or []
                              if not c.get("closed") and c.get("outcomes") and c["outcomes"][0].get("price") is not None})[:60]}
    pop = (e.get("volume24h") or 0) + 0.05 * (e.get("volume") or 0)
    return _upsert(db, "event", e["slug"], e["title"], body, e.get("image") or "", pop, data, keep_body)


def note_events(db, events):
    """Index events the app has fetched anyway (catalog pages, search fallback); full lists win."""
    try:
        with store.tx(db):
            for e in events or []:
                if isinstance(e, dict) and e.get("slug") and e.get("title"):
                    exists = db.execute("SELECT 1 FROM search_items WHERE kind='event' AND key=?", (e["slug"],)).fetchone()
                    index_event(db, e, keep_body=bool(exists))
    except Exception:  # noqa: BLE001 - indexing must never break the page that triggered it
        pass


def index_events_page(db, gap=0.35):
    """One page of the event crawl per call: the top events by 24 h volume, then the newest."""
    state = json.loads(store.kv_get(db, "search:crawl", "{}") or "{}")
    page = state.get("page", 0)
    order, offset = ("volume24hr", page * EVENT_PAGE) if page < EVENT_PAGES else ("startDate", (page - EVENT_PAGES) * EVENT_PAGE)
    params = {"active": "true", "closed": "false", "archived": "false", "limit": EVENT_PAGE, "offset": offset,
              "order": order, "ascending": "false"}
    rows = pm.fetch(pm.gamma_url("/events", params), gap=gap)
    count = 0
    with store.tx(db):
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or r.get("closed") is True:
                continue
            try:
                index_event(db, pm.event(r, full=True))
                count += 1
            except ValueError:
                continue
    page = page + 1 if count and page + 1 < EVENT_PAGES + NEW_PAGES else 0
    if page == 0:
        state["cycle"] = time.time()
        with store.tx(db):
            for (rid,) in db.execute("SELECT id FROM search_items WHERE kind='event' AND updated<?", (time.time() - STALE,)).fetchall():
                db.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
                db.execute("DELETE FROM search_items WHERE id=?", (rid,))
    state["page"] = page
    store.kv_set(db, "search:crawl", json.dumps(state))
    return count


def index_people(db):
    """Traders from the leaderboards, crawled statistics and the autopilot pool; tags from the tag census."""
    people = {}
    for r in store.rows(db.execute("SELECT board, rank, wallet, name, image, pnl, volume FROM board")):
        p = people.setdefault(r["wallet"], {"wallet": r["wallet"], "name": "", "image": "", "boards": set()})
        p["name"] = p["name"] or r["name"]
        p["image"] = p["image"] or r["image"]
        cat, period = r["board"].split(":") if ":" in r["board"] else (r["board"], "all")
        p["boards"].add(cat)
        if cat == "overall" and period == "all":
            p["rank"], p["pnl"], p["volume"] = r["rank"], r["pnl"], r["volume"]
        else:
            p.setdefault("pnl", r["pnl"])
            p.setdefault("volume", r["volume"])
    for w in store.rows(db.execute("SELECT * FROM wallets")):
        p = people.setdefault(w["wallet"], {"wallet": w["wallet"], "name": "", "image": "", "boards": set()})
        p["name"] = p["name"] or w["name"]
        p["image"] = p["image"] or w["image"]
        for k in ("win_rate", "networth", "active", "positions", "streak", "join_date", "pct_15d"):
            p[k] = w[k]
        if w["pnl"] is not None:
            p["pnl"] = w["pnl"]
        p["tags"] = traders.tags_for(w, p.get("rank"))
    for s in store.rows(db.execute("SELECT wallet, name, image, kind, metrics, score FROM scout_wallets WHERE metrics IS NOT NULL")):
        p = people.setdefault(s["wallet"], {"wallet": s["wallet"], "name": "", "image": "", "boards": set()})
        p["name"] = p["name"] or s["name"]
        p["image"] = p["image"] or s["image"]
        m = json.loads(s["metrics"])
        p["copy_wr"], p["copy_n"], p["copy_roi"], p["behaviour"], p["ap_score"] = m.get("wr"), m.get("n"), m.get("roi"), s["kind"], s["score"]
    with store.tx(db):
        seen = set()
        for w, p in people.items():
            if not p["name"] and p.get("pnl") is None:
                continue
            name = p["name"] if p["name"] and not p["name"].lower().startswith("0x") else (w[:6] + "…" + w[-4:])
            data = {k: v for k, v in p.items() if k not in ("name", "image", "boards")}
            data["boards"] = sorted(p["boards"])
            data["tags"] = p.get("tags") or traders.tags_for({}, p.get("rank"))
            _upsert(db, "trader", w, name, f"{w} {p['name']}", p["image"] or "", max(0.0, p.get("pnl") or 0), data)
            seen.add(w)
        for rid, key in db.execute("SELECT id, key FROM search_items WHERE kind='trader'").fetchall():
            if key not in seen:
                db.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
                db.execute("DELETE FROM search_items WHERE id=?", (rid,))
        tags = json.loads(store.kv_get(db, "tags", "{}") or "{}").get("rows") or []
        for t in tags:
            if t.get("slug") and t.get("label"):
                _upsert(db, "tag", t["slug"], t["label"], t["slug"].replace("-", " "), "", t.get("count") or 0,
                        {"slug": t["slug"], "count": t.get("count")})
    store.kv_set(db, "search:people", str(time.time()))
    _vocab["at"] = 0
    return len(seen)


def status(db):
    counts = dict(db.execute("SELECT kind, COUNT(*) FROM search_items GROUP BY kind").fetchall())
    crawl = json.loads(store.kv_get(db, "search:crawl", "{}") or "{}")
    return {"events": counts.get("event", 0), "traders": counts.get("trader", 0), "tags": counts.get("tag", 0),
            "cycle": crawl.get("cycle")}


# ---------------------------------------------------------------- words → index terms

def vocab(db):
    if time.time() - _vocab["at"] > 600:
        terms = {r[0] for r in db.execute("SELECT term FROM search_vocab")}
        by_first = {}
        for t in terms:
            if len(t) >= 4 and t.isalpha():
                by_first.setdefault(t[:1], []).append(t)
        _vocab.update(at=time.time(), terms=terms, by_first=by_first)
    return _vocab


def _known(v, term):
    return term in v["terms"] or (len(term) >= 4 and any(t.startswith(term) for t in v["by_first"].get(term[:1], ())))


def _fuzzy(v, word):
    if len(word) < 4 or not word.isalpha():
        return None
    hits = difflib.get_close_matches(word, v["by_first"].get(word[:1], []), n=1, cutoff=0.78)
    return hits[0] if hits else None


def terms(db, parsed):
    """Per free word, the index terms to look for (OR within a word, AND across words), plus notes."""
    v = vocab(db)
    groups, notes = [], []
    for w in parsed["words"]:
        word, alts = w["word"], set()
        if w.get("en"):
            alts.add(w["en"])
            notes.append(f"{word} → {w['en']}")
        if not qparse.lexicon.cyrillic(word):
            alts.add(word)
        for how, alt in w.get("alts") or []:
            if how in ("layout-ru", "translit-ru"):
                alts.add(alt)
                notes.append(f"{word} → {alt}")
            elif alt and _known(v, alt):
                alts.add(alt)
                notes.append(f"{word} → {alt}")
        if not w.get("en") and not any(_known(v, a) for a in alts if " " not in a):
            base = word if not qparse.lexicon.cyrillic(word) else qparse.lexicon.to_latin(word)
            fix = _fuzzy(v, base)
            if fix:
                alts.add(fix)
                notes.append(f"{word} → {fix}")
        if not alts:
            alts.add(word)
        groups.append(sorted(alts))
    return groups, list(dict.fromkeys(notes))


def _fts_term(t):
    t = t.replace('"', "")
    if " " in t:
        return f'"{t}"'
    return f'"{t}"*' if len(t) >= 3 else f'"{t}"'


def _match(db, groups, kinds, limit=400):
    if not groups:
        return []
    exprs = [" AND ".join("(" + " OR ".join(_fts_term(t) for t in g) + ")" for g in groups)]
    if len(groups) > 1:
        exprs.append(" OR ".join(_fts_term(t) for g in groups for t in g))
    marks = ",".join("?" * len(kinds))
    for expr in exprs:
        try:
            rows = store.rows(db.execute(f"""SELECT i.*, bm25(search_fts, 10.0, 1.0) AS rank FROM search_fts
                                            JOIN search_items i ON i.id=search_fts.rowid
                                            WHERE search_fts MATCH ? AND i.kind IN ({marks}) ORDER BY rank LIMIT ?""",
                                         (expr, *kinds, limit)))
        except Exception:  # noqa: BLE001 - a malformed expression simply finds nothing
            rows = []
        if rows:
            for r in rows:
                r["loose"] = expr is not exprs[0]
            return rows
    return []


# ---------------------------------------------------------------- personal signals

def _personal(db, uid):
    if not uid:
        return {"watch": set(), "held": set(), "opened": set()}
    watch = {f"{r[0]}:{r[1]}" for r in db.execute("SELECT kind, key FROM watchlist WHERE user_id=?", (uid,))}
    held = {r[0] for r in db.execute("""SELECT DISTINCT p.event_slug FROM positions p JOIN accounts a ON a.id=p.account_id
                                        WHERE a.user_id=? AND p.status='open' AND p.shares>0""", (uid,))}
    held |= {r[0] for r in db.execute("SELECT DISTINCT leader FROM copy_trades c JOIN accounts a ON a.id=c.account_id WHERE a.user_id=?", (uid,))}
    held |= {r[0] for r in db.execute("SELECT wallet FROM ap_players WHERE user_id=? AND state IN ('active','bench')", (uid,))}
    opened = {r[0] for r in db.execute("SELECT key FROM search_history WHERE user_id=? AND kind='open'", (uid,))}
    return {"watch": watch, "held": held, "opened": opened}


def _item(r, mine):
    data = json.loads(r["data"]) if isinstance(r.get("data"), str) else r.get("data") or {}
    kind = r["kind"]
    url = {"event": f"/event/{r['key']}", "trader": f"/traders/{r['key']}", "tag": f"/tags/{r['key']}"}[kind]
    wkind = {"event": "event", "trader": "trader", "tag": "tag"}[kind]
    return {"kind": kind, "key": r["key"], "title": r["title"], "image": r.get("image") or "", "url": url, "data": data,
            "pop": r.get("pop") or 0, "watched": f"{wkind}:{r['key']}" in mine["watch"],
            "mine": r["key"] in mine["held"], "opened": r["key"] in mine["opened"]}


def _rank(items, rows, raw_text, sort):
    if not items:
        return items
    rels = [-(r.get("rank") or 0) for r in rows] if rows else [0] * len(items)
    top_rel = max(rels) or 1
    pops = {}
    for it in items:
        pops[it["kind"]] = max(pops.get(it["kind"], 0), it["pop"])
    phrase = raw_text.lower()
    for it, rel in zip(items, rels):
        score = rel / top_rel if rows else 0
        if rows and rows[0].get("loose"):
            score *= 0.8
        top_pop = pops[it["kind"]] or 1
        score += 0.35 * math.log10(1 + it["pop"]) / math.log10(1 + top_pop) if top_pop > 0 else 0
        score += 0.5 * it["watched"] + 0.3 * it["mine"] + 0.2 * it["opened"]
        if phrase and it["title"].lower().startswith(phrase):
            score += 0.3
        it["score"] = round(score, 4)
    key = {
        "hot": lambda it: -(it["data"].get("volume24h") or 0), "volume": lambda it: -(it["data"].get("volume") or 0),
        "liquidity": lambda it: -(it["data"].get("liquidity") or 0), "end": lambda it: it["data"].get("end") or 9e12,
        "pnl": lambda it: -(it["data"].get("pnl") or 0), "wr": lambda it: -(_wr(it["data"]) or -1),
        "networth": lambda it: -(it["data"].get("networth") or 0), "new": lambda it: -(it["data"].get("created") or 0),
    }.get(sort)
    items.sort(key=key or (lambda it: -it["score"]))
    return items


def _wr(d):
    if d.get("copy_wr") is not None and (d.get("copy_n") or 0) >= 15:
        return d["copy_wr"] * 100
    return d.get("win_rate")


def _passes(it, f, now):
    d, kind = it["data"], it["kind"]

    def rng(value, lo, hi):
        if lo is not None and (value is None or value < lo):
            return False
        return not (hi is not None and (value is None or value > hi))

    if kind == "event":
        if f.get("category") and f["category"] not in d.get("tags", []):
            return False
        if f.get("end_before") and not (d.get("end") and now <= d["end"] <= f["end_before"]):
            return False
        if f.get("price_min") is not None or f.get("price_max") is not None:
            prices = d.get("prices") or [p for p in [(d.get("top") or [{}])[0].get("price")] if p is not None]
            if not any(rng(p, f.get("price_min"), f.get("price_max")) for p in prices):
                return False
        if not rng(d.get("volume"), f.get("volume_min"), f.get("volume_max")):
            return False
        if not rng(d.get("liquidity"), f.get("liquidity_min"), f.get("liquidity_max")):
            return False
        if f.get("new") and not (d.get("created") and d["created"] >= now - 7 * DAY):
            return False
        return True
    if kind == "trader":
        if f.get("category") and f["category"] not in d.get("boards", []):
            return False
        if not rng(_wr(d), f.get("wr_min"), f.get("wr_max")):
            return False
        if not rng(d.get("pnl"), f.get("pnl_min"), f.get("pnl_max")):
            return False
        if not rng(d.get("networth"), f.get("networth_min"), f.get("networth_max")):
            return False
        if f.get("active") and not (d.get("active") or 0) > 0:
            return False
        if f.get("behaviour") and d.get("behaviour") != f["behaviour"]:
            return False
        if f.get("tag") and f["tag"] not in (d.get("tags") or []):
            return False
        return True
    if kind == "tag":
        # a category word ("политика") has no free text to match, so without this every tag passed and the Теги
        # результаты были все 1122 тега по популярности; keep only tags of that category (1.17.2)
        if f.get("category") and categories.TAG_INDEX.get((d.get("slug") or "").lower()) != f["category"]:
            return False
        return True
    return True


KIND_MAP = {"events": "event", "traders": "trader", "tags": "tag"}


def find(db, uid, parsed, kind, filters, sort=None, limit=50, offset=0):
    """Items of one kind (events, traders or tags) for a parsed phrase with effective filters."""
    now = time.time()
    k = KIND_MAP[kind]
    groups, notes = terms(db, parsed)
    mine = _personal(db, uid)
    if groups:
        rows = _match(db, groups, [k], limit=600)
    else:
        rows = store.rows(db.execute("SELECT * FROM search_items WHERE kind=? ORDER BY pop DESC LIMIT 5000", (k,)))
    items = [_item(r, mine) for r in rows]
    keep = [(it, r) for it, r in zip(items, rows) if _passes(it, filters, now)]
    relaxed = None
    # events and traders fall back to no-category when nothing matches; tags of an empty category stay empty
    # (1.17.2: relaxing tags brought back all 1122 tags, the bug we are fixing)
    if not keep and filters.get("category") and items and k != "tag":
        f2 = {**filters, "category": None}
        keep = [(it, r) for it, r in zip(items, rows) if _passes(it, f2, now)]
        relaxed = "category" if keep else None
    items = _rank([it for it, _ in keep], [r for _, r in keep] if groups else [], parsed["raw"], sort)
    return {"rows": items[offset:offset + limit], "total": len(items), "notes": notes, "relaxed": relaxed}


# ---------------------------------------------------------------- the user's own data

def find_mine(db, uid, parsed, sub=None, limit=30):
    if not uid:
        return []
    groups, _ = terms(db, parsed)
    needles = [t.lower() for g in groups for t in g] + [w["word"] for w in parsed["words"]]

    def hit(*texts):
        if not needles:
            return True
        blob = " ".join(x or "" for x in texts).lower()
        return all(any(n in blob for n in ([w["word"]] + g)) for w, g in zip(parsed["words"], groups)) if groups else True

    out = []
    if sub in (None, "positions"):
        for r in store.rows(db.execute("""SELECT p.*, a.name AS account, a.kind AS akind FROM positions p JOIN accounts a ON a.id=p.account_id
                                          WHERE a.user_id=? AND p.status='open' AND p.shares>0 ORDER BY p.updated DESC""", (uid,))):
            if hit(r["title"], r["outcome"], r["event_slug"]):
                value = r["shares"] * (r["last_price"] or 0)
                out.append({"kind": "position", "key": f"{r['account_id']}:{r['token_id']}", "title": r["title"],
                            "url": f"/event/{r['event_slug']}?contract={r['condition_id']}", "image": "",
                            "data": {"outcome": r["outcome"], "shares": r["shares"], "value": value, "cost": r["cost"],
                                     "account": r["account"] if r["akind"] == "copy" else "Основной счёт"}})
    if sub in (None, "orders"):
        for r in store.rows(db.execute("""SELECT o.* FROM orders o JOIN accounts a ON a.id=o.account_id
                                          WHERE a.user_id=? AND o.status='open' ORDER BY o.created DESC""", (uid,))):
            if hit(r.get("title"), r.get("outcome")):
                out.append({"kind": "order", "key": str(r["id"]), "title": r.get("title") or "", "url": f"/event/{r['event_slug']}",
                            "image": "", "data": {"side": r["side"], "price": r["limit_price"], "shares": r["shares"], "amount": r["amount"],
                                     "outcome": r.get("outcome"), "type": r["type"]}})
    if sub in (None, "watchlist"):
        for r in store.rows(db.execute("SELECT * FROM watchlist WHERE user_id=? ORDER BY created DESC", (uid,))):
            if hit(r["label"], r["key"]):
                url = {"trader": f"/traders/{r['key']}", "event": f"/event/{r['key']}", "tag": f"/tags/{r['key']}"}[r["kind"]]
                out.append({"kind": "watch", "key": f"{r['kind']}:{r['key']}", "title": r["label"] or r["key"], "url": url,
                            "image": r["image"], "data": {"what": r["kind"]}})
    if sub in (None, "copies"):
        for r in store.rows(db.execute("""SELECT c.*, a.name AS profile FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                          WHERE a.user_id=? AND c.autopilot=0""", (uid,))):
            name = db.execute("SELECT title FROM search_items WHERE kind='trader' AND key=?", (r["leader"],)).fetchone()
            if hit(r["nickname"], r["leader"], name[0] if name else ""):
                out.append({"kind": "copy", "key": str(r["id"]), "title": r["nickname"] or r["leader"], "image": "",
                            "url": f"/copy-trading?profile={r['account_id']}", "data": {"profile": r["profile"], "paused": r["paused"]}})
    if sub in (None, "autopilot"):
        for r in store.rows(db.execute("""SELECT p.wallet, p.state, s.name, s.image FROM ap_players p LEFT JOIN scout_wallets s ON s.wallet=p.wallet
                                          WHERE p.user_id=? AND p.state IN ('active','bench')""", (uid,))):
            if hit(r["name"], r["wallet"]):
                out.append({"kind": "player", "key": r["wallet"], "title": r["name"] or r["wallet"], "image": r["image"] or "",
                            "url": "/autopilot" + ("?tab=bench" if r["state"] == "bench" else ""), "data": {"state": r["state"]}})
    for r in store.rows(db.execute("SELECT * FROM saved_searches WHERE user_id=? ORDER BY id DESC", (uid,))) if sub is None else []:
        if hit(r["name"], r["query"]):
            out.append({"kind": "saved", "key": str(r["id"]), "title": r["name"], "image": "", "url": f"/search?saved={r['id']}",
                        "data": {"query": r["query"], "notify": r["notify"]}})
    return out[:limit]


# ---------------------------------------------------------------- trades

def find_trades(db, parsed, filters, limit=80):
    """Recent trades on events matching the phrase (or the busiest events), filtered by size, side, outcome, period."""
    groups, notes = terms(db, parsed)
    if groups:
        rows = _match(db, groups, ["event"], limit=40)
    else:
        rows = store.rows(db.execute("SELECT * FROM search_items WHERE kind='event' ORDER BY pop DESC LIMIT 200"))
    events = [r for r in rows if not filters.get("category") or filters["category"] in json.loads(r["data"]).get("tags", [])][:6]
    since = time.time() - filters["period_h"] * 3600 if filters.get("period_h") else 0
    out, scanned = [], []
    for ev in events:
        data = json.loads(ev["data"])
        if not data.get("id"):
            continue
        scanned.append({"slug": ev["key"], "title": ev["title"]})
        try:
            page = pm.trades(db, event_id=data["id"], side=filters.get("side") or "", limit=200, taker_only=True)["data"]
        except (pm.UpstreamError, LookupError, ValueError):
            continue
        for t in (page or {}).get("rows") or []:
            amount = (t.get("size") or 0) * (t.get("price") or 0)
            if amount < (filters.get("amount_min") or 0) or (filters.get("amount_max") and amount > filters["amount_max"]):
                continue
            if filters.get("outcome") and str(t.get("outcome") or "").lower() != filters["outcome"].lower():
                continue
            if since and (t.get("timestamp") or 0) < since:
                continue
            out.append({**t, "amount": amount, "event_title": ev["title"], "event_image": ev["image"]})
    out.sort(key=lambda t: -(t.get("timestamp") or 0))
    return {"rows": out[:limit], "total": len(out), "notes": notes, "scanned": scanned}


# ---------------------------------------------------------------- public entry points

def effective(parsed, params):
    """Filters from the phrase, overridden by explicit facet values from the page ("none" clears one)."""
    f = {k: v for k, v in parsed["filters"].items()}
    for key, raw in (params or {}).items():
        if raw in (None, ""):
            continue
        if raw == "none":
            f.pop(key, None)
            continue
        if key in ("category", "side", "outcome", "behaviour", "tag", "mine"):
            f[key] = raw
        elif key in ("active", "new", "whale"):
            f[key] = raw in ("1", "true", True)
        elif key == "end_days":
            f["end_before"] = time.time() + float(raw) * DAY
        else:
            v = pm.num(raw)
            if v is not None:
                f[key] = v
    return f


def quick(db, uid, raw):
    """Palette results: a few of each kind, best first, with the interpretation of the phrase."""
    parsed = qparse.parse(raw)
    out = {"parsed": _public(parsed), "groups": {}, "notes": [], "go": parsed["go"]}
    if parsed["go"]:
        return out
    if not raw.strip():
        return out
    kind = parsed["kind"]
    f = parsed["filters"]
    sizes = {"events": 6, "traders": 5, "tags": 3} if kind is None else {kind: 10} if kind in KIND_MAP else {}
    for k, n in sizes.items():
        res = find(db, uid, parsed, k, f if kind else {}, parsed["sort"], limit=n)
        out["groups"][k] = {"rows": [_slim(r) for r in res["rows"]], "total": res["total"], "relaxed": res["relaxed"]}
        out["notes"] += res["notes"]
    if uid and (kind in (None, "mine")):
        out["groups"]["mine"] = {"rows": find_mine(db, uid, parsed, f.get("mine"), limit=6 if kind is None else 20)}
    if kind == "trades":
        out["trades"] = True
    out["notes"] = list(dict.fromkeys(out["notes"]))
    if kind is None and sum(len(g["rows"]) for g in out["groups"].values()) < 2 and parsed["words"]:
        out["groups"]["events"]["rows"] += [_slim(x) for x in _fallback(db, uid, parsed)]
    return out


def full(db, uid, raw, kind=None, params=None, limit=50, offset=0, record=False):
    parsed = qparse.parse(raw)
    kind = kind or parsed["kind"] or "all"
    f = effective(parsed, params)
    sort = (params or {}).get("sort") or parsed["sort"]
    out = {"parsed": _public(parsed), "kind": kind, "filters": f, "sort": sort, "go": parsed["go"]}
    if record and uid and raw.strip():
        remember(db, uid, "query", raw.strip(), raw.strip())
    if kind == "all":
        out["groups"] = {}
        notes = []
        for k, n in (("events", 8), ("traders", 8), ("tags", 8)):
            res = find(db, uid, parsed, k, f, sort, limit=n)
            out["groups"][k] = {"rows": res["rows"], "total": res["total"], "relaxed": res["relaxed"]}
            notes += res["notes"]
        if uid:
            out["groups"]["mine"] = {"rows": find_mine(db, uid, parsed, f.get("mine"), limit=10)}
        if parsed["words"] and out["groups"]["events"]["total"] < 3:
            extra = _fallback(db, uid, parsed)
            out["groups"]["events"]["rows"] += extra
            out["groups"]["events"]["total"] += len(extra)
        out["notes"] = list(dict.fromkeys(notes))
        return out
    if kind == "trades":
        out.update(find_trades(db, parsed, f, limit=limit))
        return out
    if kind == "mine":
        rows = find_mine(db, uid, parsed, f.get("mine"), limit=200)
        out.update(rows=rows, total=len(rows), notes=terms(db, parsed)[1])
        return out
    res = find(db, uid, parsed, kind, f, sort, limit=limit, offset=offset)
    if kind == "events" and parsed["words"] and res["total"] < 3 and not offset:
        extra = [x for x in _fallback(db, uid, parsed) if _passes(x, f, time.time())]
        res["rows"] += extra
        res["total"] += len(extra)
    out.update(res)
    return out


def _fallback(db, uid, parsed):
    """Polymarket's own search for words the index does not know yet; results are indexed for next time."""
    words = []
    for w in parsed["words"]:
        alt = next((a for how, a in w.get("alts") or [] if how in ("layout", "translit")), None)
        words.append(w.get("en") or (alt if qparse.lexicon.cyrillic(w["word"]) and alt else w["word"]))
    phrase = " ".join(words) or parsed["raw"]
    try:
        res = pm.search(db, phrase[:120])["data"]
    except (pm.UpstreamError, LookupError, ValueError):
        return []
    events = res.get("events") or []
    note_events(db, events)
    mine = _personal(db, uid)
    out = []
    for e in events[:6]:
        row = store.one(db.execute("SELECT * FROM search_items WHERE kind='event' AND key=?", (e["slug"],)))
        if row:
            it = _item(row, mine)
            it["score"] = 0
            it["external"] = True
            out.append(it)
    return out


def _slim(it):
    d = it["data"]
    keep = {"event": ("top", "volume", "volume24h", "end", "tag_labels", "count", "slug"),
            "trader": ("pnl", "win_rate", "copy_wr", "copy_n", "networth", "tags", "rank", "behaviour", "ap_score", "active"),
            "tag": ("count",)}.get(it["kind"], ())
    return {**{k: v for k, v in it.items() if k != "data"}, "data": {k: d.get(k) for k in keep}}


def _public(parsed):
    return {"raw": parsed["raw"], "kind": parsed["kind"], "chips": parsed["chips"],
            "words": [{"word": w["word"], "en": w.get("en")} for w in parsed["words"]], "sort": parsed["sort"]}


# ---------------------------------------------------------------- history, home, saved searches, alerts

def remember(db, uid, kind, key, title, subtitle="", image="", url=""):
    db.execute("""INSERT INTO search_history(user_id, kind, key, title, subtitle, image, url, at) VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(user_id, kind, key) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle,
                  image=excluded.image, url=excluded.url, at=excluded.at""",
               (uid, kind, str(key)[:300], str(title)[:300], str(subtitle)[:300], str(image)[:600], str(url)[:300], time.time()))
    db.execute("""DELETE FROM search_history WHERE user_id=? AND kind=? AND key NOT IN
                  (SELECT key FROM search_history WHERE user_id=? AND kind=? ORDER BY at DESC LIMIT 30)""", (uid, kind, uid, kind))


def visit(db, uid, body):
    kind = body.get("kind")
    if kind not in ("event", "trader", "tag", "position", "order", "watch", "copy", "player", "saved", "page"):
        raise ValueError("Неизвестный тип")
    url = str(body.get("url") or "")
    if not url.startswith("/"):
        raise ValueError("Ссылка должна вести внутрь приложения")
    remember(db, uid, "open", f"{kind}:{body.get('key')}", body.get("title") or "", body.get("subtitle") or "",
             body.get("image") or "", url)
    return {"ok": True}


def home(db, uid):
    out = {"recent": [], "opened": [], "saved": [], "trending": [], "top_traders": [], "index": status(db)}
    if uid:
        out["recent"] = [r["title"] for r in store.rows(db.execute(
            "SELECT title FROM search_history WHERE user_id=? AND kind='query' ORDER BY at DESC LIMIT 8", (uid,)))]
        out["opened"] = store.rows(db.execute("""SELECT key, title, subtitle, image, url, at FROM search_history
                                                 WHERE user_id=? AND kind='open' ORDER BY at DESC LIMIT 6""", (uid,)))
        out["saved"] = saved(db, uid)
    mine = _personal(db, uid)
    out["trending"] = [_slim(_item(r, mine)) for r in store.rows(db.execute(
        "SELECT * FROM search_items WHERE kind='event' ORDER BY json_extract(data,'$.volume24h') DESC LIMIT 5"))]
    out["top_traders"] = [_slim(_item(r, mine)) for r in store.rows(db.execute(
        "SELECT * FROM search_items WHERE kind='trader' ORDER BY json_extract(data,'$.pct_15d') DESC LIMIT 5"))]
    return out


def clear_history(db, uid):
    db.execute("DELETE FROM search_history WHERE user_id=?", (uid,))
    return {"ok": True}


SAVED_KINDS = {"all", "events", "traders", "trades", "tags", "mine"}


def saved(db, uid):
    rows = store.rows(db.execute("SELECT * FROM saved_searches WHERE user_id=? ORDER BY pinned DESC, id DESC", (uid,)))
    for r in rows:
        r["params"] = json.loads(r["params"] or "{}")
        r.pop("seen", None)
    return rows


def save(db, uid, body):
    name = str(body.get("name") or body.get("query") or "").strip()[:80]
    query = str(body.get("query") or "").strip()[:200]
    kind = body.get("kind") or "all"
    params = {k: str(v)[:40] for k, v in (body.get("params") or {}).items() if v not in (None, "")}
    if not name or (not query and not params):
        raise ValueError("Нужны запрос и название")
    if kind not in SAVED_KINDS:
        raise ValueError("Неизвестный раздел поиска")
    if db.execute("SELECT COUNT(*) FROM saved_searches WHERE user_id=?", (uid,)).fetchone()[0] >= 50:
        raise ValueError("Можно сохранить до 50 поисков")
    rid = db.execute("""INSERT INTO saved_searches(user_id, name, query, kind, params, pinned, notify, created)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (uid, name, query, kind, json.dumps(params), int(bool(body.get("pinned", True))),
                      int(bool(body.get("notify"))), time.time())).lastrowid
    return store.one(db.execute("SELECT id, name, query, kind, params, pinned, notify FROM saved_searches WHERE id=?", (rid,)))


def update_saved(db, uid, body):
    rid = int(body.get("id") or 0)
    row = db.execute("SELECT id FROM saved_searches WHERE id=? AND user_id=?", (rid, uid)).fetchone()
    if not row:
        raise LookupError("Поиск не найден")
    if "name" in body:
        name = str(body["name"]).strip()[:80]
        if not name:
            raise ValueError("Нужно название")
        db.execute("UPDATE saved_searches SET name=? WHERE id=?", (name, rid))
    for key in ("pinned", "notify"):
        if key in body:
            db.execute(f"UPDATE saved_searches SET {key}=? WHERE id=?", (int(bool(body[key])), rid))
    if body.get("notify"):
        db.execute("UPDATE saved_searches SET seen='[]', checked=NULL WHERE id=? AND notify=1 AND checked IS NULL", (rid,))
    return {"ok": True}


def delete_saved(db, uid, rid):
    db.execute("DELETE FROM saved_searches WHERE id=? AND user_id=?", (int(rid or 0), uid))
    return {"ok": True}


def _keys(db, row):
    res = full(db, row["user_id"], row["query"], row["kind"] if row["kind"] != "all" else None,
               json.loads(row["params"] or "{}"), limit=60)
    if "groups" in res:
        items = [it for g in res["groups"].values() for it in g["rows"]]
    else:
        items = res.get("rows") or []
    out = []
    for it in items:
        if "transaction_hash" in it or "timestamp" in it:
            key = it.get("transaction_hash") or f"{it.get('timestamp')}:{it.get('proxy_wallet')}:{it.get('token_id')}"
            title = f"{it.get('name') or it.get('pseudonym') or str(it.get('proxy_wallet'))[:10]}: {it.get('side')} {it.get('outcome')} · " \
                    f"${it.get('amount', 0):,.0f} · {it.get('event_title') or it.get('title')}"
        else:
            key, title = f"{it.get('kind')}:{it.get('key')}", it.get("title")
        out.append((key, title))
    return out


def check_alerts(db, now=None, limit=10):
    """New matches of saved searches with alerts: notify once per new item, at most three per message."""
    now = now or time.time()
    rows = store.rows(db.execute("""SELECT * FROM saved_searches WHERE notify=1 AND (checked IS NULL OR checked<?)
                                    ORDER BY checked IS NOT NULL, checked LIMIT ?""", (now - 300, limit)))
    sent = 0
    for row in rows:
        try:
            found = _keys(db, row)
        except (pm.UpstreamError, LookupError, ValueError):
            db.execute("UPDATE saved_searches SET checked=? WHERE id=?", (now, row["id"]))
            continue
        seen = set(json.loads(row["seen"] or "[]"))
        fresh = [(k, t) for k, t in found if k not in seen]
        if row["checked"] is not None and fresh:
            titles = "; ".join(t for _, t in fresh[:3]) + (f" и ещё {len(fresh) - 3}" if len(fresh) > 3 else "")
            notify.add(db, row["user_id"], "search", f"Новое по поиску «{row['name']}»", titles, f"/search?saved={row['id']}")
            sent += 1
        seen |= {k for k, _ in found}
        db.execute("UPDATE saved_searches SET seen=?, checked=?, hits=hits+? WHERE id=?",
                   (json.dumps(sorted(seen)[-800:]), now, len(fresh) if row["checked"] is not None else 0, row["id"]))
    return sent


def suggest_name(raw):
    return re.sub(r"\s+", " ", raw).strip()[:60]
