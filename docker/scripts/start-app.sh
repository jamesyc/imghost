#!/bin/sh
set -eu

if [ "${STORAGE_BACKEND:-filesystem}" = "garage" ]; then
    python -m imghost init-storage
fi

exec python -m uvicorn imghost.main:app --host 0.0.0.0 --port 8000
