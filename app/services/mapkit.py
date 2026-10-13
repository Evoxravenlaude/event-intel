"""
Apple MapKit JS authorization tokens.

MapKit JS asks the page for a JWT on load (and again when it expires). The token
is an ES256 JWT signed with the Maps private key from the Apple Developer portal:

    header  {"alg": "ES256", "kid": <key id>, "typ": "JWT"}
    payload {"iss": <team id>, "iat": now, "exp": now + ttl, "origin": <site>}

`origin` locks the token to our site so it can't be lifted and reused elsewhere.
Tokens are cached and reissued shortly before expiry.
"""
from __future__ import annotations

import threading
import time

import jwt

from app.core.config import settings

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


class MapKitNotConfigured(RuntimeError):
    pass


def _private_key() -> str:
    key = (settings.apple_mapkit_private_key or "").strip().strip('"').replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" not in key:
        # allow pasting just the base64 body of the .p8 file
        body = "".join(key.split())
        key = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END PRIVATE KEY-----\n"
    return key


def token(origin: str | None = None) -> str:
    if not settings.mapkit_configured:
        raise MapKitNotConfigured("Apple MapKit is not configured")
    origin = settings.apple_mapkit_origin or origin
    now = time.time()
    with _lock:
        hit = _cache.get(origin or "")
        if hit and hit[1] - now > 120:
            return hit[0]
        ttl = max(300, settings.apple_mapkit_token_ttl_seconds)
        payload = {"iss": settings.apple_mapkit_team_id, "iat": int(now), "exp": int(now + ttl)}
        if origin:
            payload["origin"] = origin
        tok = jwt.encode(payload, _private_key(), algorithm="ES256",
                         headers={"kid": settings.apple_mapkit_key_id, "typ": "JWT"})
        _cache[origin or ""] = (tok, now + ttl)
        return tok


def reset() -> None:
    with _lock:
        _cache.clear()
