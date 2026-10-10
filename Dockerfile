# ── Build stage: install dependencies separately for layer caching ──────────
FROM python:3.13-slim AS builder

WORKDIR /app

# Set to "false" to build a ~1GB-smaller image without torch/sentence-transformers.
# The app detects the missing package and falls back to Jaccard similarity.
ARG WITH_EMBEDDINGS=true

# System deps needed by psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ml.txt ./
RUN if [ "$WITH_EMBEDDINGS" = "true" ]; then \
        pip install --no-cache-dir --prefix=/install -r requirements.txt ; \
    else \
        grep -v '^-r requirements-ml.txt' requirements.txt > /tmp/req.txt \
        && pip install --no-cache-dir --prefix=/install -r /tmp/req.txt ; \
    fi


# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

# Runtime system libs only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    # Create a non-root user — never run as root in production
    && addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code (see .dockerignore for what is excluded)
COPY --chown=appuser:appgroup . .

RUN mkdir -p /app/.cache/huggingface && chown -R appuser:appgroup /app/.cache \
    && chmod +x scripts/bootstrap.sh

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HF_HOME=/app/.cache/huggingface

EXPOSE 8000

# bootstrap.sh runs `alembic upgrade head` then starts uvicorn.
# It is a bash script (uses `set -o pipefail`), so it must NOT be run with `sh`
# — on Debian `sh` is dash, which rejects pipefail and exits immediately.
CMD ["bash", "scripts/bootstrap.sh"]
