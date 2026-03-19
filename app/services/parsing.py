from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from app.schemas.event import RawSignalCreate

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "web3": [
        # English
        "web3", "blockchain", "crypto", "dao", "zk", "onchain", "defi", "nft", "wallet", "token",
        # French
        "chaîne de blocs", "jeton", "cryptomonnaie",
        # Pidgin / informal
        "crypto thing", "web3 thing",
    ],
    "tech": [
        # English
        "developer", "dev", "engineering", "python", "javascript", "ai", "machine learning",
        "meetup", "hackathon", "software", "startup tech", "open source", "devfest", "google i/o",
        # French (common in Francophone West Africa)
        "développeur", "logiciel", "intelligence artificielle", "atelier numérique",
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
        "summit", "conference", "networking", "startup", "venture", "founder", "panel",
        "investor", "pitch", "accelerator", "entrepreneur",
        # French
        "conférence", "sommet", "réseautage", "entrepreneur", "investisseur",
        "affaires", "forum économique",
        # Pidgin
        "business people", "make money", "hustle", "connect with people",
        # Swahili
        "biashara", "mkutano", "mtandao",
    ],
    "culture": [
        # English
        "festival", "exhibition", "music", "concert", "fashion", "art", "film",
        "performance", "gallery", "afrobeats", "afrotech",
        # French
        "festival", "exposition", "musique", "mode", "spectacle",
        # Pidgin / informal
        "vibes", "turn up", "party", "show", "perform",
        # Yoruba
        "ìdárayá", "orin", "ìgbádùn",
        # Swahili
        "tamasha", "muziki", "sanaa",
    ],
}

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


def infer_category(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for category, words in CATEGORY_KEYWORDS.items():
        if any(word in lowered for word in words):
            return category
    return "general"


def infer_structure_type(source_type: str) -> str:
    if source_type in {"luma", "eventbrite", "linkedin"}:
        return "structured"
    if source_type in {"telegram", "x"}:
        return "semi-structured"
    return "unstructured"


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def extract_text_from_html(html: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    paragraphs = " ".join(node.get_text(" ", strip=True) for node in soup.find_all(["p", "article", "section"]))
    return clean_text(title), clean_text(paragraphs)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def contains_live_word(text: str | None) -> bool:
    """Return True if the text contains any LIVE_WORDS phrase."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in LIVE_WORDS)


def infer_times_from_text(text: str | None) -> tuple[datetime | None, datetime | None]:
    if not text:
        return None, None
    lowered = text.lower()
    now = datetime.now(timezone.utc)
    # "tomorrow" in English and "demain" in French
    if "tomorrow" in lowered or "demain" in lowered:
        start = now + timedelta(days=1, hours=18)
        return start, start + timedelta(hours=3)
    # LIVE_WORDS, "tonight"/"ce soir", "today"/"aujourd'hui" all imply same-day
    if contains_live_word(text) or "tonight" in lowered or "today" in lowered \
            or "ce soir" in lowered or "aujourd'hui" in lowered:
        start = now + timedelta(hours=2)
        return start, start + timedelta(hours=3)
    return None, None


def build_external_id(url: str | None, title: str | None) -> str | None:
    if url:
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"[:255]
    if title:
        return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:255]
    return None


def html_to_signal(source: str, source_name: str, url: str, html: str, city: str | None = None) -> RawSignalCreate:
    title, body = extract_text_from_html(html)
    start_time, end_time = infer_times_from_text(body or title)
    text_blob = f"{title or ''} {body or ''}"
    # Boost base confidence when live-word phrases are present
    base_confidence = 0.55
    if contains_live_word(text_blob):
        base_confidence = min(base_confidence + LIVE_WORD_CONFIDENCE_BOOST, 1.0)
    return RawSignalCreate(
        source_type=source,
        source_name=source_name,
        external_id=build_external_id(url, title),
        title=title,
        body=body,
        location_text=city,
        url=url,
        detected_start_time=start_time,
        detected_end_time=end_time,
        source_confidence=base_confidence,
        normalized_category=infer_category(text_blob),
    )
