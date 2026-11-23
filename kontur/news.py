"""Live news for an event from Google News and Bing News RSS search (merged, newest first)."""

import html
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlsplit
from xml.etree import ElementTree

from . import polymarket as pm

FEEDS = {
    "Google News": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
    "Bing News": "https://www.bing.com/news/search?q={q}&format=rss",
}
NOISE = re.compile(r"\b(will|the|a|an|of|in|on|by|be|to|who|what|which|win|winner|polymarket|odds)\b|[?:()—–-]", re.I)


def query_for(title):
    words = NOISE.sub(" ", title)
    words = " ".join(w for w in words.split() if len(w) > 1)[:120]
    return words or title[:120]


def _real_link(link):
    """Bing wraps article links in apiclick.aspx?url=...; keep the publisher's address."""
    parts = urlsplit(link)
    if "bing.com" in parts.netloc and "apiclick" in parts.path:
        target = parse_qs(parts.query).get("url", [""])[0]
        if target.startswith("http"):
            return target
    return link


def _parse(body, provider):
    items = []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return items
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        source = (item.findtext("source") or "").strip()
        if not source:
            for child in item:
                if child.tag.endswith("Source") or child.tag.endswith("source"):
                    source = (child.text or "").strip()
        if provider == "Google News" and " - " in title and not source:
            title, source = title.rsplit(" - ", 1)
        elif provider == "Google News" and source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            at = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            at = None
        items.append({"title": title[:300], "link": _real_link(link)[:1500], "source": source[:120] or provider,
                      "provider": provider, "at": at})
    return items


def for_event(db, title):
    q = query_for(title)

    def load():
        found, errors = [], 0
        for provider, template in FEEDS.items():
            try:
                body, _ = pm.fetch(template.format(q=quote_plus(q)), accept="application/rss+xml, application/xml, text/xml",
                                   raw=True, limit=3_000_000, timeout=10)
                found += _parse(body, provider)
            except (pm.UpstreamError, LookupError):
                errors += 1
        if errors == len(FEEDS):
            raise pm.UpstreamError("Новостные ленты недоступны")
        seen, out = set(), []
        for item in sorted(found, key=lambda i: -(i["at"] or 0)):
            key = re.sub(r"\W+", " ", item["title"].lower()).strip()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return {"query": q, "items": out[:40], "at": time.time()}
    return pm.cached(db, "news:" + q, 300, load)
