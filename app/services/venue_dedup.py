"""
Venue deduplication via fuzzy name matching.

When a new venue name arrives we:
1. Check exact match (existing behaviour, fast path).
2. Check the alias table (scoped to the same city when one is given).
3. Load all venues in the same city and score them with SequenceMatcher.
4. If the best match exceeds FUZZY_THRESHOLD, treat it as the same venue and
   record the incoming name as a VenueAlias for future lookups.
5. Otherwise create a new venue.

This handles common real-world variants:
  "Landmark Event Centre"  vs  "Landmark Events Centre, Lagos"
  "iHub Nairobi"           vs  "iHub"
  "The Civic Centre"       vs  "Civic Centre Lagos"
"""
from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Venue, VenueAlias

# A match ratio at or above this threshold is treated as the same venue.
# 0.82 catches typical punctuation/word-order variations without over-merging.
FUZZY_THRESHOLD = 0.82

_FILLERS = (
    "centre", "center", "hall", "venue", "complex", "the", "hub",
    # cities commonly appended to venue names
    "lagos", "nairobi", "accra", "abuja", "kigali", "dakar", "libreville", "cairo", "johannesburg",
)
_FILLER_RE = re.compile(r"\b(?:" + "|".join(_FILLERS) + r")\b")


def _normalise(name: str) -> str:
    """Lowercase, strip accents/punctuation and filler words for comparison."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = _FILLER_RE.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


def _ratio(a: str, b: str) -> float:
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def find_fuzzy_venue(db: Session, name: str, city: str | None) -> Venue | None:
    """
    Return an existing Venue that is a fuzzy match for `name` in `city`,
    or None if no match exceeds FUZZY_THRESHOLD.
    """
    # Alias table first — exact alias hits are always correct, but only within
    # the same city: "iHub" in Lagos must not resolve to iHub Nairobi.
    alias_stmt = select(VenueAlias).join(Venue, VenueAlias.venue_id == Venue.id).where(VenueAlias.alias == name)
    if city:
        alias_stmt = alias_stmt.where(Venue.city.ilike(city))
    alias_hit = db.execute(alias_stmt).scalars().first()
    if alias_hit:
        return alias_hit.venue

    stmt = select(Venue)
    if city:
        stmt = stmt.where(Venue.city.ilike(city))
    candidates: list[Venue] = list(db.execute(stmt).scalars().all())

    best: Venue | None = None
    best_ratio = 0.0
    for venue in candidates:
        r = _ratio(name, venue.name)
        if r > best_ratio:
            best_ratio = r
            best = venue

    if best and best_ratio >= FUZZY_THRESHOLD:
        return best
    return None


def record_alias(db: Session, venue: Venue, alias: str, source: str | None = None) -> None:
    """
    Persist a new alias for an existing venue if it isn't already recorded.
    Silently skips if the alias already exists.
    """
    if alias == venue.name:
        return
    existing = db.execute(
        select(VenueAlias).where(VenueAlias.venue_id == venue.id, VenueAlias.alias == alias)
    ).scalar_one_or_none()
    if existing:
        return
    db.add(VenueAlias(venue_id=venue.id, alias=alias, source=source))
    # Caller is responsible for flushing/committing
