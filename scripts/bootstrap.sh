#!/usr/bin/env bash
# API service entrypoint: apply migrations, then serve.
# Run with `bash scripts/bootstrap.sh` (not `sh` — dash does not support pipefail).
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-$PWD}"

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "[bootstrap] running alembic upgrade head"
  alembic upgrade head
else
  echo "[bootstrap] RUN_MIGRATIONS=false — skipping migrations"
fi

WORKERS="${WEB_CONCURRENCY:-1}"
echo "[bootstrap] starting uvicorn on port ${PORT:-8000} (workers=${WORKERS})"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "$WORKERS" --proxy-headers --forwarded-allow-ips='*'
