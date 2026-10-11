"""
API key authentication middleware.

Set API_KEY in your environment to enable key enforcement.
When API_KEY is not set (default in development), all requests pass through.

The key may be sent as `X-API-Key: <key>` or `Authorization: Bearer <key>`.

Exempt paths — never require a key:
  /health, /ready   — uptime / readiness probes
  /                 — Meridian frontend (served as index.html)
  /static/*         — frontend assets
  /docs, /redoc, /openapi.json — API docs (only mounted when DEBUG=true anyway)
  OPTIONS *         — CORS preflight requests carry no custom headers

Keys are compared with hmac.compare_digest to prevent timing attacks.
"""
from __future__ import annotations
import hmac
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings

# Paths that never require authentication
EXEMPT_PREFIXES = ("/static/", "/docs", "/redoc", "/openapi.json")
EXEMPT_EXACT = {"/", "/health", "/ready", "/robots.txt", "/favicon.ico"}


def _is_exempt(path: str) -> bool:
    if path in EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


def _extract_key(request: Request) -> str:
    key = request.headers.get("X-API-Key", "")
    if key:
        return key
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.api_key:
            # Auth disabled — pass through (dev/test mode)
            return await call_next(request)

        if request.method == "OPTIONS" or _is_exempt(request.url.path):
            return await call_next(request)

        incoming = _extract_key(request)
        if not incoming or not hmac.compare_digest(incoming.encode(), settings.api_key.encode()):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
                headers={"WWW-Authenticate": "ApiKey"},
            )

        return await call_next(request)
