"""
Outbound webhook service.

When an event's confidence score crosses WEBHOOK_CONFIDENCE_THRESHOLD, this
service fires a POST to every URL registered in WEBHOOK_URLS (comma-separated).

Delivery is fire-and-forget — failures are logged but never raise to the caller.
Each call is made in a background thread so it never blocks the request cycle.

Payload shape:
{
  "event": "event.confirmed",
  "event_id": 42,
  "title": "Lagos Web3 Summit",
  "status": "upcoming",
  "confidence_score": 0.87,
  "category": "web3",
  "start_time": "2026-06-15T18:00:00+00:00"
}
"""
from __future__ import annotations
import logging
import threading
from datetime import datetime

import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

# Confidence threshold above which an event is considered "confirmed"
WEBHOOK_CONFIDENCE_THRESHOLD = 0.75


def _deliver(url: str, payload: dict) -> None:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url, json=payload)
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
) -> None:
    """
    Fire webhooks if the confidence score exceeds the threshold.
    Called after clustering or review resolution — safe to call unconditionally.
    """
    urls = settings.split_csv(settings.webhook_urls)
    if not urls or confidence_score < WEBHOOK_CONFIDENCE_THRESHOLD:
        return

    payload = {
        "event": "event.confirmed",
        "event_id": event_id,
        "title": title,
        "status": status,
        "confidence_score": round(confidence_score, 3),
        "category": category,
        "start_time": start_time.isoformat() if start_time else None,
    }

    for url in urls:
        # Each delivery is a daemon thread — won't block shutdown
        t = threading.Thread(target=_deliver, args=(url, payload), daemon=True)
        t.start()
