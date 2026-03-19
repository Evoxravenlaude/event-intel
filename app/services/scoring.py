from __future__ import annotations
from datetime import datetime, timezone


def infer_status(start_time, end_time, confidence: float) -> str:
    now = datetime.now(timezone.utc)
    if start_time and start_time > now:
        return "upcoming"
    if start_time and end_time and start_time <= now <= end_time:
        return "ongoing" if confidence >= 0.7 else "likely_ongoing"
    if end_time and now > end_time:
        return "ended"
    return "uncertain"


def score_event(source_confidence: float, evidence_count: int, geo_score: float, time_score: float, organizer_score: float = 0.0) -> float:
    score = source_confidence * 0.35
    score += min(evidence_count, 5) * 0.1
    score += geo_score * 0.2
    score += time_score * 0.2
    score += organizer_score * 0.15
    return round(min(score, 1.0), 3)
