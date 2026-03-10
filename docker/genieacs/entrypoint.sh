#!/usr/bin/env bash
set -euo pipefail

: "${GENIEACS_MONGODB_CONNECTION_URL:?GENIEACS_MONGODB_CONNECTION_URL is required}"
: "${GENIEACS_UI_JWT_SECRET:=change-me}"

pids=()

cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait || true
}

trap cleanup INT TERM

genieacs-cwmp &
pids+=("$!")

genieacs-nbi &
pids+=("$!")

genieacs-fs &
pids+=("$!")

genieacs-ui &
pids+=("$!")

wait -n "${pids[@]}"
status=$?
cleanup
exit "$status"
