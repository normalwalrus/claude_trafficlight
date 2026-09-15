#!/bin/sh
# Install the host hooks, then serve. Bootstrap failures are logged, never
# fatal: a server that starts without hooks is far more useful than one that
# refuses to start at all.
set -e

python /app/bootstrap.py || echo "[entrypoint] bootstrap failed, continuing"

exec uvicorn app:app --host 0.0.0.0 --port 8787 --log-level warning
