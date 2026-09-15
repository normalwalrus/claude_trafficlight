#!/usr/bin/env sh
# Thin wrapper around the cross-platform installer (macOS / Linux).
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
py=$(command -v python3 || command -v python) || {
    echo "ERROR: python3 is not on PATH. Install Python 3.8+ and retry." >&2
    exit 1
}
exec "$py" "$root/install.py" "$@"
