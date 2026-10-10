"""
Outbound webhook service.

When an event's confidence score *crosses* CONFIRMED_CONFIDENCE_THRESHOLD
(from below to at-or-above), this service POSTs to every URL registered in
WEBHOOK_URLS (comma-separated). It used to fire on every signal linked to an
already-confirmed event, which spammed subscribers with duplicates.

Delivery is fire-and-forget — failures are logged but never raise to the caller.
Each call is made in a daemon thread so it never blocks the request cycle.
If WEBHOOK_SECRET is set, the body is signed: `X-Signature: sha256=<hmac-hex>`.

Payload shape:
{
  "event": "event.confirmed",
  "event_id": 42,
  "title": "Lagos Web3 Summit",
  "status": "upcoming",
  "confidence_score": 0.87,
  "category": "web3",
  "start_time": "2026-06-15T18:00:00+00:00",
  "city": "Lagos",
  "evidence_count": 3
}
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from app.core.config import settings

if TYPE_CHECKING:
    from app.models.event import Event

logger = logging.getLogger(__name__)

# Kept for backward compatibility with imports; the live value is in settings.
WEBHOOK_CONFIDENCE_THRESHOLD = settings.confirmed_confidence_threshold


def _deliver(url: str, body: bytes, headers: dict[str, str]) -> None:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url, content=body, headers=headers)
            response.raise_for_status()
            logger.info("Webhook delivered to %s (status %d)", url, response.status_code)
    except Exception as exc:
        logger.warning("Webhook delivery failed for %s: %s", url, exc)


def fire_event_confirmed(
    event_id: int,
    title: str,
    status: str,
    confidence_score: float,
    category: str | None,
    start_time: datetime | None,
    city: str | None = None,
    evidence_count: int | None = None,
) -> int:
    """
    Fire webhooks unconditionally for the given event. Returns the number of
    deliveries started. Callers should normally use `fire_if_newly_confirmed`.
    """
    urls = settings.split_csv(settings.webhook_urls)
    if not urls:
        return 0

    payload = {
        "event": "event.confirmed",
        "event_id": event_id,
        "title": title,
        "status": status,
        "confidence_score": round(confidence_score, 3),
        "category": category,
        "start_time": start_time.isoformat() if start_time else None,
        "city": city,
        "evidence_count": evidence_count,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "event-intel-webhook/1.0"}
    if settings.webhook_secret:
        sig = hmac.new(settings.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Signature"] = f"sha256={sig}"

    for url in urls:
        threading.Thread(target=_deliver, args=(url, body, headers), daemon=True).start()
    return len(urls)


def fire_if_newly_confirmed(event: "Event", confidence_before: float, confidence_after: float) -> bool:
    """Fire only when the event crossed the confirmed threshold in this change."""
    threshold = settings.confirmed_confidence_threshold
    if confidence_before >= threshold or confidence_after < threshold:
        return False
    fire_event_confirmed(
        event_id=event.id,
        title=event.title,
        status=event.status,
        confidence_score=confidence_after,
        category=event.category,
        start_time=event.start_time,
        city=event.venue.city if event.venue else None,
        evidence_count=event.evidence_count,
    )
    return True
