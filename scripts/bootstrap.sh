#!/usr/bin/env bash
set -euo pipefail
cd /app
PYTHONPATH=/app alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
