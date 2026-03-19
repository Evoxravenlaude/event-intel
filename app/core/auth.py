"""
API key authentication middleware.

Set API_KEY in your environment to enable key enforcement.
When API_KEY is not set (default in development), all requests pass through.

Exempt paths — never require a key:
  /health      — uptime monitors
  /            — Meridian frontend (served as index.html)
  /static/*    — frontend assets
  /docs        — Swagger UI (only visible when DEBUG=true anyway)
  /openapi.json

Keys are compared with hmac.compare_digest to prevent timing attacks.
"""
from __future__ import annotations
import hmac
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings

# Paths that never require authentication
EXEMPT_PREFIXES = ("/health", "/static/", "/docs", "/redoc", "/openapi.json")
EXEMPT_EXACT    = {"/", "/health"}


def _is_exempt(path: str) -> bool:
    if path in EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.api_key:
            # Auth disabled — pass through (dev/test mode)
            return await call_next(request)

        if _is_exempt(request.url.path):
            return await call_next(request)

        incoming = request.headers.get("X-API-Key", "")
        if not incoming or not hmac.compare_digest(incoming, settings.api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
