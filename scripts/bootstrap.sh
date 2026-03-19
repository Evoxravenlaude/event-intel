#!/usr/bin/env bash
set -euo pipefail
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
