"""
Text parsing helpers shared by every source adapter.

- infer_category(): keyword → category with *word-boundary* matching.
  (Plain substring matching produced "ai" ⊂ "Nairobi" → tech, "art" ⊂ "party"
  → culture, "dao" ⊂ "Daoud" → web3. Every Nairobi event was "tech".)
- extract_jsonld_events(): schema.org Event objects embedded in a page.
  Luma, Eventbrite and LinkedIn event pages all ship these; it is far more
  reliable than scraping <p> tags.
- html_to_signal(): page → RawSignalCreate, preferring JSON-LD, falling back
  to title/paragraph text.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from app.schemas.event import RawSignalCreate

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "web3": [
        # English
        "web3", "blockchain", "crypto", "dao", "zk", "onchain", "on-chain", "defi", "nft", "nfts",
        "wallet", "token", "tokens", "ethereum", "solana", "bitcoin", "stablecoin", "airdrop",
        # French
        "chaîne de blocs", "jeton", "cryptomonnaie",
        # Pidgin / informal
        "crypto thing", "web3 thing",
    ],
    "tech": [
        # English
        "developer", "developers", "dev", "devs", "engineering", "python", "javascript", "ai",
        "machine learning", "meetup", "hackathon", "software", "startup tech", "open source",
        "devfest", "google i/o", "data science", "cloud", "api",
        # French (common in Francophone West Africa)
        "développeur", "développeurs", "logiciel", "intelligence artificielle", "atelier numérique",
        "technologie", "codage", "programmation",
        # Nigerian Pidgin
        "tech bros", "make we code", "build something", "techie",
        # Swahili (East Africa)
        "teknolojia", "programu", "ubunifu",
        # Yoruba
        "ìmọ̀ ẹrọ",
    ],
    "business": [
        # English
        "summit", "conference", "networking", "startup", "startups", "venture", "founder", "founders",
        "panel", "investor", "investors", "pitch", "accelerator", "entrepreneur", "entrepreneurs",
        "fintech", "b2b",
        # French
        "conférence", "sommet", "réseautage", "entrepreneur", "investisseur",
        "affaires", "forum économique",
        # Pidgin
        "business people", "make money", "hustle", "connect with people",
        # Swahili
        "biashara", "mkutano", "mtandao", "wawekezaji",
    ],
    "culture": [
        # English
        "festival", "exhibition", "music", "concert", "fashion", "art", "arts", "film",
        "performance", "gallery", "afrobeats", "afrotech", "comedy", "theatre", "theater",
        # French
        "exposition", "musique", "mode", "spectacle",
        # Pidgin / informal
        "show", "perform",
        # Yoruba
        "ìdárayá", "orin", "ìgbádùn",
        # Swahili
        "tamasha", "muziki", "sanaa",
    ],
    "sports": [
        "match", "football", "soccer", "basketball", "tennis", "marathon", "run club", "5k", "10k",
        "tournament", "league", "cup final", "boxing", "mma", "cricket", "rugby", "cycling", "esports",
        "fitness", "yoga", "gym", "swim",
        # French / Swahili
        "football", "course", "mpira", "mbio",
    ],
    "food": [
        "food festival", "tasting", "brunch", "dinner", "wine", "cocktail", "chef", "street food",
        "cooking class", "pop-up kitchen", "jollof", "suya", "barbecue", "bbq",
        # French / Swahili
        "dégustation", "gastronomie", "chakula",
    ],
    "nightlife": [
        "nightlife", "club night", "dj", "afterparty", "after-party", "rave", "late night", "lounge",
        "turn up", "vibes", "party",
        # French
        "soirée", "boîte de nuit",
    ],
    "wellness": [
        "wellness", "meditation", "mindfulness", "retreat", "breathwork", "mental health", "self-care",
        "therapy session", "sound bath",
        # French
        "bien-être", "méditation",
    ],
    "community": [
        "community", "volunteer", "cleanup", "clean-up", "town hall", "fundraiser", "charity",
        "support group", "neighbourhood", "neighborhood", "church", "mosque", "worship",
        # French / Swahili
        "bénévolat", "communauté", "jamii", "kujitolea",
    ],
    "education": [
        "workshop", "masterclass", "bootcamp", "training", "lecture", "seminar", "webinar", "class",
        "course", "tutorial", "study group", "book club",
        # French / Swahili
        "atelier", "formation", "cours", "mafunzo", "darasa",
    ],
}

# Public list of categories (used by the API docs and the frontend filter chips)
CATEGORIES: list[str] = list(CATEGORY_KEYWORDS) + ["general"]

# Phrases that indicate an event is happening imminently or right now.
# Includes Pidgin and common informal markers used across Lagos / Accra social posts.
LIVE_WORDS: list[str] = [
    # English
    "live now", "happening now", "today", "tonight", "doors open", "pull up",
    "starting soon", "just started", "come through", "we live",
    # Nigerian Pidgin
    "make we go", "na today", "e don start", "come join us", "dey happen now",
    # French (used in Francophone cities)
    "en cours", "aujourd'hui", "ce soir", "c'est maintenant",
]

# Confidence boost applied when a live-word is detected
LIVE_WORD_CONFIDENCE_BOOST = 0.12

# Column widths in the raw_signals table — anything longer is a Postgres error.
MAX_TITLE = 255
MAX_LOCATION = 255
MAX_URL = 1000
MAX_EXTERNAL_ID = 255


def _compile(words: list[str]) -> re.Pattern:
    r"""
    Build one regex that matches any keyword as a whole word/phrase.
    \b doesn't work for strings that start/end with non-word characters
    (e.g. "google i/o"), so use explicit (?<!\w) / (?!\w) guards instead.
    """
    alts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)


_CATEGORY_PATTERNS: dict[str, re.Pattern] = {cat: _compile(words) for cat, words in CATEGORY_KEYWORDS.items()}
_LIVE_PATTERN = _compile(LIVE_WORDS)


def truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def infer_category(text: str | None) -> str | None:
    """
    Return the first category whose keyword list has a whole-word hit.
    Ties are broken by number of distinct keyword hits so a "fintech
    conference about blockchain" lands on web3/business by weight rather
    than dict order.
    """
    if not text:
        return None
    best_cat, best_score = None, 0.0
    for category, pattern in _CATEGORY_PATTERNS.items():
        matches = set(m.lower() for m in pattern.findall(text))
        # Multi-word phrases are more specific than single words: "street food"
        # should beat "festival". Remaining ties fall to dict order (web3 → tech → …).
        score = sum(1.5 if " " in m else 1.0 for m in matches)
        if score > best_score:
            best_cat, best_score = category, score
    return best_cat or "general"


def infer_structure_type(source_type: str) -> str:
    if source_type in {"luma", "eventbrite", "linkedin", "ticketmaster", "seatgeek"}:
        return "structured"
    if source_type in {"telegram", "x"}:
        return "semi-structured"
    return "unstructured"


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def strip_html(value: str | None) -> str | None:
    if not value:
        return None
    return clean_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def extract_text_from_html(html: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"]
    elif soup.title and soup.title.string:
        title = soup.title.string
    desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    paragraphs = " ".join(node.get_text(" ", strip=True) for node in soup.find_all(["p", "article", "section"]))
    body = paragraphs or (desc.get("content") if desc else None)
    return clean_text(title), clean_text(body)


def parse_datetime(value) -> datetime | None:
    """Parse ISO strings, RFC-2822 feed dates, or date/datetime objects → aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        try:
            dt = date_parser.parse(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def contains_live_word(text: str | None) -> bool:
    """Return True if the text contains any LIVE_WORDS phrase."""
    if not text:
        return False
    return bool(_LIVE_PATTERN.search(text))


def infer_times_from_text(text: str | None) -> tuple[datetime | None, datetime | None]:
    if not text:
        return None, None
    lowered = text.lower()
    now = datetime.now(timezone.utc)
    # "tomorrow" in English and "demain" in French
    if re.search(r"(?<!\w)(tomorrow|demain)(?!\w)", lowered):
        start = now + timedelta(days=1, hours=18)
        return start, start + timedelta(hours=3)
    # LIVE_WORDS ("tonight", "today", "ce soir", "aujourd'hui", …) imply same-day
    if contains_live_word(text):
        start = now + timedelta(hours=2)
        return start, start + timedelta(hours=3)
    return None, None


def build_external_id(url: str | None, title: str | None) -> str | None:
    """
    Stable per-item identifier for deduplication.

    Prefers the URL (netloc + path) when it identifies the item. When several
    items share one URL (an ICS feed, a listing page) callers must pass the
    item's own id instead — see `stable_id`.
    """
    if url:
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"[:MAX_EXTERNAL_ID]
    if title:
        return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:MAX_EXTERNAL_ID]
    return None


def stable_id(*parts) -> str:
    """Deterministic short hash of the given parts — for items with no native id."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


# ---------------------------------------------------------------------------
# schema.org / JSON-LD
# ---------------------------------------------------------------------------

_EVENT_TYPES = {
    "Event", "SocialEvent", "BusinessEvent", "EducationEvent", "Festival",
    "MusicEvent", "ExhibitionEvent", "ScreeningEvent", "SalesEvent", "Hackathon",
}


def _walk_jsonld(node, out: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out)
    elif isinstance(node, dict):
        types = node.get("@type")
        if isinstance(types, str):
            types = [types]
        if types and any(t in _EVENT_TYPES for t in types):
            out.append(node)
        # @graph and nested nodes
        for key in ("@graph", "itemListElement", "item", "subEvent"):
            if key in node:
                _walk_jsonld(node[key], out)


def extract_jsonld_events(html: str) -> list[dict]:
    """Return every schema.org Event object found in <script type=ld+json>."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            # Some sites emit several JSON objects back to back
            try:
                data = json.loads("[" + re.sub(r"\}\s*\{", "},{", raw) + "]")
            except ValueError:
                continue
        _walk_jsonld(data, found)
    return found


def _jsonld_location(data: dict) -> tuple[str | None, float | None, float | None]:
    loc = data.get("location")
    if isinstance(loc, list):
        loc = next((l for l in loc if isinstance(l, dict)), None)
    if not isinstance(loc, dict):
        return (clean_text(loc) if isinstance(loc, str) else None), None, None
    address = loc.get("address")
    if isinstance(address, dict):
        addr_text = ", ".join(
            filter(None, [address.get("streetAddress"), address.get("addressLocality"), address.get("addressCountry")])
        ) or None
        locality = address.get("addressLocality")
    else:
        addr_text = clean_text(address) if isinstance(address, str) else None
        locality = None
    name = loc.get("name") or locality or addr_text
    geo = loc.get("geo") or {}
    lat = lng = None
    try:
        if geo.get("latitude") is not None and geo.get("longitude") is not None:
            lat, lng = float(geo["latitude"]), float(geo["longitude"])
    except (TypeError, ValueError):
        lat = lng = None
    return clean_text(name), lat, lng


def jsonld_event_to_signal(
    data: dict, *, source: str, source_name: str, url: str, city: str | None, confidence: float,
) -> RawSignalCreate | None:
    title = clean_text(data.get("name"))
    if not title:
        return None
    description = strip_html(data.get("description")) if data.get("description") else None
    start_dt = parse_datetime(data.get("startDate"))
    end_dt = parse_datetime(data.get("endDate"))
    location_text, lat, lng = _jsonld_location(data)
    page_url = data.get("url") if isinstance(data.get("url"), str) else url
    organizer = data.get("organizer")
    if isinstance(organizer, dict):
        organizer = organizer.get("name")
    body = description or ""
    if isinstance(organizer, str) and organizer:
        body = f"{body}\n\nOrganizer: {organizer}".strip()
    return RawSignalCreate(
        source_type=source,
        source_name=source_name,
        external_id=(build_external_id(page_url, None) if page_url != url else None)
        or stable_id(source, url, title, start_dt),
        title=truncate(title, MAX_TITLE),
        body=body or None,
        location_text=truncate(location_text or city, MAX_LOCATION),
        url=truncate(page_url, MAX_URL),
        detected_start_time=start_dt,
        detected_end_time=end_dt,
        latitude=lat,
        longitude=lng,
        source_confidence=confidence,
        normalized_category=infer_category(f"{title} {description or ''}"),
    )


def html_to_signal(source: str, source_name: str, url: str, html: str, city: str | None = None) -> RawSignalCreate:
    """Page → signal. Uses the first schema.org Event if present, else page text."""
    for data in extract_jsonld_events(html):
        sig = jsonld_event_to_signal(data, source=source, source_name=source_name, url=url, city=city, confidence=0.8)
        if sig:
            return sig

    title, body = extract_text_from_html(html)
    start_time, end_time = infer_times_from_text(body or title)
    text_blob = f"{title or ''} {body or ''}"
    base_confidence = 0.55
    if contains_live_word(text_blob):
        base_confidence = min(base_confidence + LIVE_WORD_CONFIDENCE_BOOST, 1.0)
    return RawSignalCreate(
        source_type=source,
        source_name=source_name,
        external_id=build_external_id(url, title),
        title=truncate(title, MAX_TITLE),
        body=body,
        location_text=truncate(city, MAX_LOCATION),
        url=truncate(url, MAX_URL),
        detected_start_time=start_time,
        detected_end_time=end_time,
        source_confidence=base_confidence,
        normalized_category=infer_category(text_blob),
    )
