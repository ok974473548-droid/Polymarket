"""Words for the search: Russian → English terms of Polymarket, categories, keyboard layout and transliteration.

Keys are lower-case Russian stems: a query word matches a key when the key is the word itself or the word
with up to three ending letters cut off ("франции" → "франц"). Values are English words looked up in the index.
"""

# --- Russian stems → English search terms ---------------------------------------------------------------
TERMS = {
    # politics and people
    "трамп": "trump", "байден": "biden", "харрис": "harris", "вэнс": "vance", "венс": "vance", "обам": "obama",
    "ньюсом": "newsom", "десантис": "desantis", "рубио": "rubio", "маск": "musk", "илон": "elon", "безос": "bezos",
    "цукерберг": "zuckerberg", "альтман": "altman", "путин": "putin", "зеленск": "zelensky", "зеленский": "zelensky",
    "нетаньях": "netanyahu", "си цзиньпин": "xi", "си": "xi", "моди": "modi", "макрон": "macron", "ле пен": "le pen",
    "лепен": "le pen", "барделл": "bardella", "мелончон": "melenchon", "меланшон": "melenchon", "мерц": "merz",
    "шольц": "scholz", "стармер": "starmer", "фарадж": "farage", "мелони": "meloni", "эрдоган": "erdogan",
    "лула": "lula", "милей": "milei", "болсонар": "bolsonaro", "карни": "carney", "трюдо": "trudeau",
    "мамдани": "mamdani", "кеннеди": "kennedy", "пелоси": "pelosi", "шумер": "schumer", "пауэлл": "powell",
    "уорш": "warsh", "хассет": "hassett", "папа римск": "pope", "папа": "pope",
    "президент": "president", "премьер": "prime minister", "канцлер": "chancellor", "губернатор": "governor",
    "мэр": "mayor", "сенат": "senate", "конгресс": "congress", "палат": "house", "парламент": "parliament",
    "выбор": "election", "праймериз": "primary", "номинац": "nomination", "номинант": "nominee", "кандидат": "candidate",
    "демократ": "democrat", "республиканц": "republican", "партия": "party", "партии": "party", "голосован": "vote",
    "импичмент": "impeachment", "отставк": "resign", "помилован": "pardon", "тариф": "tariff", "пошлин": "tariff",
    "санкци": "sanctions", "указ": "executive order", "суд": "court", "верховн": "supreme", "арест": "arrest",
    "депортац": "deportation", "шатдаун": "shutdown", "референдум": "referendum", "коалиц": "coalition",
    # countries, places
    "сша": "us", "америк": "america", "росси": "russia", "украин": "ukraine", "кита": "china", "китай": "china",
    "франц": "france", "французск": "french", "германи": "germany", "немецк": "german", "британи": "uk",
    "великобритани": "uk", "англи": "england", "израил": "israel", "иран": "iran", "газ": "gaza", "палестин": "palestine",
    "ливан": "lebanon", "сири": "syria", "турци": "turkey", "инди": "india", "япони": "japan", "коре": "korea",
    "тайван": "taiwan", "канад": "canada", "мексик": "mexico", "бразили": "brazil", "аргентин": "argentina",
    "венесуэл": "venezuela", "итали": "italy", "испани": "spain", "польш": "poland", "венгри": "hungary",
    "нидерланд": "netherlands", "голланди": "netherlands", "португали": "portugal", "грузи": "georgia",
    "европ": "europe", "евросоюз": "eu", "нато": "nato", "оон": "un", "нью-йорк": "new york", "нью йорк": "new york",
    "калифорни": "california", "техас": "texas", "флорид": "florida", "вашингтон": "washington", "лондон": "london",
    "париж": "paris", "москв": "moscow", "киев": "kyiv", "майами": "miami", "чикаго": "chicago", "гренланди": "greenland",
    "панам": "panama", "куб": "cuba", "кубок": "cup",
    # war and world
    "войн": "war", "перемири": "ceasefire", "мирн": "peace", "мир": "peace", "вторжен": "invasion", "удар": "strike",
    "ракет": "missile", "ядерн": "nuclear", "заложник": "hostage", "переговор": "talks", "сделк": "deal",
    "встреч": "meeting", "саммит": "summit",
    # economy and finance
    "фрс": "fed", "федрезерв": "fed", "ставк": "rate", "процентн": "interest", "снижен": "cut", "повышен": "hike",
    "инфляц": "inflation", "рецесс": "recession", "ввп": "gdp", "безработиц": "unemployment", "занятост": "jobs",
    "нефт": "oil", "золот": "gold", "серебр": "silver", "акци": "stock", "индекс": "index", "доллар": "dollar",
    "евро": "euro", "рубл": "ruble", "ецб": "ecb", "банк": "bank", "ипо": "ipo", "капитализац": "market cap",
    "нвиди": "nvidia", "эппл": "apple", "майкрософт": "microsoft", "гугл": "google", "тесл": "tesla", "амазон": "amazon",
    "опенэйай": "openai", "антропик": "anthropic",
    # crypto
    "биткоин": "bitcoin", "биткойн": "bitcoin", "битко": "bitcoin", "бтк": "btc", "эфир": "ethereum", "эфириум": "ethereum",
    "солан": "solana", "догикоин": "dogecoin", "доги": "doge", "рипл": "xrp", "крипт": "crypto", "монет": "coin",
    "стейблкоин": "stablecoin", "этф": "etf", "халвинг": "halving", "майкростратеджи": "microstrategy",
    # sport
    "футбол": "soccer", "чемпионат мира": "world cup", "чм": "world cup", "лига чемпионов": "champions league",
    "лч": "champions league", "лига европы": "europa league", "премьер-лиг": "premier league", "апл": "premier league",
    "ла лиг": "la liga", "серия а": "serie a", "бундеслиг": "bundesliga", "нба": "nba", "нфл": "nfl", "нхл": "nhl",
    "млб": "mlb", "суперкубок": "super bowl", "супербоул": "super bowl", "плей-офф": "playoffs", "плейофф": "playoffs",
    "финал": "final", "матч": "vs", "победител": "winner", "чемпион": "champion", "теннис": "tennis", "уимблдон": "wimbledon",
    "формул": "f1", "гонк": "grand prix", "бокс": "boxing", "юфс": "ufc", "бой": "fight", "олимпиад": "olympics",
    "гольф": "golf", "хоккей": "hockey", "баскетбол": "basketball", "бейсбол": "baseball", "крикет": "cricket",
    "реал": "real madrid", "барселон": "barcelona", "манчестер": "manchester", "арсенал": "arsenal", "челси": "chelsea",
    "ливерпул": "liverpool", "бавари": "bayern", "ювентус": "juventus", "пср": "psg", "лейкерс": "lakers",
    "киберспорт": "esports", "дота": "dota", "кс": "counter-strike", "контр-страйк": "counter-strike",
    # culture, tech, other
    "оскар": "oscars", "грэмми": "grammy", "фильм": "movie", "кино": "box office", "сборы": "box office", "песн": "song",
    "альбом": "album", "евровидени": "eurovision", "свадьб": "wedding", "развод": "divorce", "беременн": "pregnant",
    "ии": "ai", "искусственн": "ai", "нейросет": "ai", "модел": "model", "релиз": "release", "запуск": "launch",
    "спейсикс": "spacex", "марс": "mars", "луна": "moon", "погод": "weather", "температур": "temperature",
    "жар": "hottest", "снег": "snow", "ураган": "hurricane", "землетрясен": "earthquake", "пандеми": "pandemic",
    "вирус": "virus", "твит": "tweet", "твиттер": "twitter", "упомина": "mention", "скажет": "say", "скажут": "say",
    # verbs and common words of market titles
    "победит": "win", "выиграет": "win", "выигра": "win", "проиграет": "lose", "станет": "next", "следующ": "next",
    "уйдет": "out", "покинет": "out", "цена": "price", "курс": "price", "достигнет": "reach", "выше": "above",
    "ниже": "below", "больше": "above", "меньше": "below", "год": "year", "месяц": "month", "недел": "week",
    "январ": "january", "феврал": "february", "март": "march", "апрел": "april", "ма": "may", "июн": "june",
    "июл": "july", "август": "august", "сентябр": "september", "октябр": "october", "ноябр": "november",
    "декабр": "december",
}

# --- categories: Russian stem → (tag slug used by Gamma and the leaderboards, label) ---------------------
CATEGORIES = {
    "политик": ("politics", "Политика"), "спорт": ("sports", "Спорт"), "крипт": ("crypto", "Крипто"),
    "культур": ("culture", "Культура"), "поп-культур": ("culture", "Культура"), "экономик": ("economy", "Экономика"),
    "финанс": ("finance", "Финансы"), "технолог": ("tech", "Технологии"), "геополитик": ("geopolitics", "Геополитика"),
    "киберспорт": ("esports", "Киберспорт"), "погод": ("weather", "Погода"), "выборн": ("elections", "Выборы"),
}
CATEGORY_LABELS = {slug: label for slug, label in CATEGORIES.values()}

# --- keyboard layouts ----------------------------------------------------------------------------------
_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю."
EN_TO_RU = str.maketrans(_EN, _RU)
RU_TO_EN = str.maketrans(_RU, _EN)

# --- transliteration (Cyrillic → Latin, Latin → Cyrillic) -----------------------------------------------
_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
       "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
       "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
_TR_BACK = [("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
            ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
            ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
            ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "у"), ("x", "кс"), ("y", "й"), ("z", "з")]


def to_latin(word):
    return "".join(_TR.get(ch, ch) for ch in word)


def to_cyrillic(word):
    out, i = [], 0
    while i < len(word):
        for lat, cyr in _TR_BACK:
            if word.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(word[i])
            i += 1
    return "".join(out)


STOP = {
    "в", "во", "на", "по", "до", "и", "или", "за", "с", "со", "что", "кто", "к", "ко", "у", "о", "об", "из", "от", "для", "про",
    "это", "ли", "же", "все", "всё", "самые", "самый", "самая", "мне", "покажи", "показать", "найди", "найти", "ищу",
    "который", "которые", "какой", "какие", "где", "когда", "как", "бы", "а", "но", "не", "the", "a", "an", "of", "in",
    "on", "will", "by", "to", "is", "be", "and", "or", "for", "at", "with", "его", "ее", "её", "их", "там", "тут",
}


def cyrillic(word):
    return any("а" <= ch <= "я" or ch == "ё" for ch in word)


def stems(word):
    """The word and its forms with up to three last letters cut, longest first."""
    out = [word]
    for cut in (1, 2, 3):
        if len(word) - cut >= 2:
            out.append(word[:-cut])
    return out


def _fits(stem, word):
    """Short stems only match short words: "ма" is May in "мая", but not part of "мама"."""
    cut = len(word) - len(stem)
    return cut == 0 or len(stem) >= 4 or (len(stem) == 3 and cut <= 2) or (len(stem) == 2 and cut <= 1)


def translate(word):
    """English term for a Russian word, or None."""
    for s in stems(word):
        if s in TERMS and _fits(s, word):
            return TERMS[s]
    return None


def category(word):
    for s in stems(word):
        if s in CATEGORIES and _fits(s, word):
            return CATEGORIES[s]
    return None
