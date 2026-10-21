#!/bin/sh
# Human-run wrapper for apply.py. Usage: ./apply.sh [--dry-run]
# Requires OPENBAO_ADDR (validated) and, for a real apply, OPENBAO_TOKEN.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/apply.py" "$@"
