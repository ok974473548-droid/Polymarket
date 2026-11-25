"""Category of a Polymarket event, for judging a player's skill category by category (1.16.0).

A player can be strong in politics or esports and weak in weather or the economy; the autopilot copies a buy only
in categories where the player's skill is proven (see scout.cat_stats and autopilot.copy_buy).

The category comes from the event's tags (Gamma, cached in `event_tags`) when they are known, else from its slug and
title, which on Polymarket say what the event is ("highest-temperature-in-…", "cs2-…", "will-trump-say-…").
On 2 000 random events of the research base the slug and title rules agree with the tags in 9 cases of 10
(tests/test_categories.py checks a sample).
"""
import re

CATS = {
    "politics": "Политика",
    "geopolitics": "Геополитика",
    "sports": "Спорт",
    "esports": "Киберспорт",
    "crypto": "Крипто",
    "finance": "Экономика и финансы",
    "weather": "Погода",
    "culture": "Культура и упоминания",
    "tech": "Наука и технологии",
    "other": "Прочее",
}

# tag slug -> category, checked in this order (an esports match is also tagged "sports", a Trump tweet "politics")
TAGS = [
    ("esports", {"esports", "counter-strike-2", "counter-strike", "cs2", "league-of-legends", "lol", "valorant", "dota-2",
                 "dota", "overwatch", "rocket-league", "call-of-duty", "starcraft", "rainbow-six"}),
    ("weather", {"weather", "daily-temperature", "highest-temperature", "lowest-temperature", "temperature", "climate",
                 "hurricanes", "hurricane", "snow", "rain", "precipitation", "natural-disasters", "earthquakes"}),
    ("crypto", {"crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "xrp", "dogecoin", "memecoins", "airdrops",
                "stablecoins", "defi", "nft", "crypto-policy", "pump-fun", "hyperliquid"}),
    ("culture", {"mention-markets", "mentions", "tweets-markets", "tweet-markets", "pop-culture", "movies", "music",
                 "celebrities", "awards", "oscars", "grammys", "tv", "entertainment", "youtube", "mrbeast", "culture",
                 "box-office", "netflix", "taylor-swift", "eurovision", "religion", "pope"}),
    ("geopolitics", {"geopolitics", "world", "iran", "israel", "ukraine", "russia", "china", "middle-east", "gaza",
                     "war", "nato", "ceasefire", "foreign-policy", "venezuela", "taiwan", "north-korea", "syria"}),
    ("politics", {"politics", "elections", "us-presidential-election", "midterms", "global-elections", "trump",
                  "house-elections", "senate-elections", "main-election", "us-politics", "congress", "governor",
                  "primaries", "mayoral-elections", "uk-politics", "courts", "scotus", "supreme-court", "trump-presidency"}),
    ("finance", {"finance", "economy", "equities", "stocks", "stock-prices", "earnings", "fed", "fed-rates",
                 "interest-rates", "inflation", "cpi", "gdp", "jobs", "unemployment", "commodities", "gold", "oil",
                 "big-tech", "ipos", "business", "tariffs", "recession", "finance-updown", "pyth-finance", "forex"}),
    ("tech", {"tech", "ai", "science", "space", "spacex", "openai", "elon-musk", "apple", "chatgpt", "gpt-5",
              "tesla", "starship", "nasa"}),
    ("sports", {"sports", "soccer", "tennis", "baseball", "basketball", "nba", "nfl", "mlb", "nhl", "hockey", "ufc",
                "mma", "boxing", "golf", "cricket", "f1", "formula1", "cfb", "ncaa", "epl", "ucl", "football",
                "premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "mls", "chess", "cycling", "wnba"}),
]
TAG_INDEX = {}
for _cat, _tags in TAGS:
    for _t in _tags:
        TAG_INDEX.setdefault(_t.lower(), _cat)
ORDER = [c for c, _ in TAGS]

# slug or title -> category when no tags are known (same order of checks)
RULES = [
    ("esports", r"\b(cs2|counter-strike|lol|league of legends|valorant|dota|overwatch|rocket league|esports|bo[1-5])\b"
                r"|^(cs2|lol|val|dota2?|ow)-"),
    ("weather", r"temperature|°[cf]\b|\b(rain|snow|hurricanes?|precipitation|tornado(es)?|heat ?wave|wind gust)\b|earthquake"),
    # a match: "A vs B" with a date in the slug, exact scores, halves, transfers (before countries and coins)
    ("sports", r"\bvs\.?\b.*\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}.*\bvs\.?\b|exact score|\b1st half\b|\b2nd half\b|"
               r"\btransfer\b|\bjoin [A-Z]|\bmatch\b|halftime|titled tuesday|\bchess\b"),
    ("crypto", r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge(coin)?|crypto|memecoin|pump\.fun|hyperliquid|airdrop|"
               r"fdv|token|stablecoin|microstrategy)\b"),
    ("culture", r"\b(say|says|said|mention|mentions|tweet|tweets|post|posts|oscars?|academy awards?|nominated|grammy|emmy|"
                r"golden globe|movie|album|song|box office|"
                r"netflix|youtube|mrbeast|spotify|billboard|eurovision|pope|time person of the year)\b"),
    ("geopolitics", r"\b(iran|israel|ukraine|russia|putin|zelensky|china|xi jinping|gaza|hamas|hezbollah|ceasefire|nato|"
                    r"invade|invasion|strike on|missile|venezuela|maduro|taiwan|north korea|kim jong|syria|war)\b"),
    ("politics", r"\b(election|elections|president|presidential|senate|house|governor|mayor|primary|nominee|"
                 r"republican|democrat|gop|trump|biden|harris|vance|newsom|parliament|prime minister|chancellor|"
                 r"cabinet|impeach|supreme court|scotus|poll|approval rating|vote|referendum)\b"),
    ("finance", r"\b(fed|fomc|interest rate|rates|inflation|cpi|gdp|recession|unemployment|jobs report|payrolls|"
                r"s&p|nasdaq|dow|stock|stocks|shares|earnings|ipo|market cap|gold|oil|tariff|tariffs|treasury|"
                r"yield|nvidia|apple|tesla|microsoft|amazon|google|meta|revenue|eps|valuation|fda|close at|"
                r"microstrategy|national debt|central bank|reserve bank|ecb|boe)\b|\$[A-Z]{1,5}\b|\([A-Z]{2,5}\)"),
    ("tech", r"\b(ai|openai|chatgpt|gpt|gemini|claude|anthropic|spacex|starship|nasa|launch|elon musk|"
             r"neuralink|robotaxi|iphone|quantum)\b"),
    ("sports", r"\bvs\.?\b|\b(win on|spread|o/u|over/under|nba|nfl|mlb|nhl|ufc|fifa|uefa|premier league|champions league|"
               r"la liga|serie a|bundesliga|ligue 1|grand prix|f1|atp|wta|open|cup|championship|super bowl|world series|"
               r"stanley cup|playoffs|mvp|ballon d'or|draft|knockout|round of)\b"
               r"|^(nba|nfl|mlb|nhl|epl|ucl|uel|atp|wta|ufc|cbb|cfb|mls|lal|bun|sea|fl1|ita|arg|bra|tur|ere|por|kbo|npb)-"),
]
_RULES = [(c, re.compile(rx, re.I)) for c, rx in RULES]


def of_tags(tags):
    """Category from tag slugs, or None if none of them is known."""
    found = {TAG_INDEX.get(str(t).lower()) for t in tags or ()} - {None}
    for c in ORDER:
        if c in found:
            return c
    return None


def of_text(slug, title=""):
    """Category from an event's slug and title; "other" when nothing fits."""
    text = f"{(slug or '').replace('-', ' ')} {title or ''}"
    for c, rx in _RULES:
        if rx.search(text) or rx.search(slug or ""):
            return c
    return "other"


def of_event(db, slug, title="", fetch=False):
    """Category of one event: from cached tags, from Gamma when `fetch` (one request), else from its words."""
    return of_events(db, {slug: title}, fetch=fetch).get(slug, "other")


def of_events(db, titles, fetch=False):
    """{slug: category} for {slug: title}: tags known in the database first (all of them at once with `fetch`)."""
    from . import polymarket as pm
    slugs = [s for s in titles if s]
    tags = {}
    if fetch and slugs:
        tags = pm.events_tags(db, slugs)
    elif slugs:
        for i in range(0, len(slugs), 500):
            part = slugs[i:i + 500]
            q = ",".join("?" * len(part))
            for s, t in db.execute(f"SELECT event_slug, tags FROM event_tags WHERE event_slug IN ({q})", part):
                import json
                tags[s] = json.loads(t or "[]")
    out = {}
    for s in slugs:
        out[s] = of_tags(tags.get(s)) or of_text(s, titles.get(s) or "")
    return out
