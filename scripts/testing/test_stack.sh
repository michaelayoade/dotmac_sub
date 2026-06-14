#!/usr/bin/env bash
#
# test_stack.sh — manage a disposable "dotmac_test" stack for Playwright/manual
# edge-case testing. It NEVER touches the live `dotmac_sub` database: it creates a
# separate `dotmac_test` database on the same local Postgres container and runs a
# SECOND app instance on port 8010 (the live app stays on 8001).
#
# All secrets are derived at runtime from `.env` and the running containers — none
# are hardcoded here.
#
# Usage:
#   scripts/testing/test_stack.sh create     # create dotmac_test DB + extensions
#   scripts/testing/test_stack.sh migrate    # alembic upgrade head (working-tree alembic)
#   scripts/testing/test_stack.sh seed        # load edge-case fixtures
#   scripts/testing/test_stack.sh up          # (re)start the test app on :8010
#   scripts/testing/test_stack.sh down        # stop+remove the test app container
#   scripts/testing/test_stack.sh logs        # tail test app logs
#   scripts/testing/test_stack.sh psql [SQL]  # psql into dotmac_test (superuser)
#   scripts/testing/test_stack.sh status      # show what's running
#   scripts/testing/test_stack.sh bootstrap   # create + migrate + seed + up
#   scripts/testing/test_stack.sh reset        # DROP dotmac_test then bootstrap
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

# --- config -----------------------------------------------------------------
PG_CONTAINER="dotmac_pg_local"
NETWORK="dotmac_sub_default"
IMAGE="dotmac_sub-app"
APP_CONTAINER="dotmac_test_app"
TEST_DB="dotmac_test"
HOST_PORT="8010"
REDIS_DB_INDEX="5"   # isolate from the live app's settings cache / sessions (db 0)

# --- derive secrets from .env + the running pg container --------------------
[ -f .env ] || { echo "no .env in $REPO" >&2; exit 1; }
# live DATABASE_URL (dotmac_app DSN) — swap the db name to dotmac_test
LIVE_DB_URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)"
TEST_DB_URL="${LIVE_DB_URL%/*}/${TEST_DB}"
DB_OWNER="$(printf '%s' "$LIVE_DB_URL" | sed -E 's#.*://([^:/]+).*#\1#')"
REDIS_PW="$(grep -E '^REDIS_LOCAL_PASSWORD=' .env | head -1 | cut -d= -f2-)"
REDIS_URL="redis://:${REDIS_PW}@redis-local:6379/${REDIS_DB_INDEX}"
PG_SUPER_PW="$(docker exec "$PG_CONTAINER" printenv POSTGRES_PASSWORD)"
PG_SUPER_USER="$(docker exec "$PG_CONTAINER" printenv POSTGRES_USER)"

psql_super() { # args: -d DB -c SQL ...
  docker exec -e PGPASSWORD="$PG_SUPER_PW" "$PG_CONTAINER" psql -U "$PG_SUPER_USER" "$@"
}

run_in_image() { # runs a command in a one-off app container w/ test env + mounts
  docker run --rm --network "$NETWORK" --env-file .env \
    -e DATABASE_URL="$TEST_DB_URL" \
    -e REDIS_URL="$REDIS_URL" \
    -e SESSION_REDIS_URL="$REDIS_URL" \
    -e CELERY_BROKER_URL="$REDIS_URL" \
    -e APP_ENV=development \
    -v "$REPO/app:/app/app" \
    -v "$REPO/alembic:/app/alembic" \
    -v "$REPO/alembic.ini:/app/alembic.ini" \
    -v "$REPO/scripts:/app/scripts" \
    --entrypoint sh "$IMAGE" -lc "$1"
}

cmd_create() {
  echo ">> creating database $TEST_DB"
  if psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$TEST_DB'" | grep -q 1; then
    echo "   already exists"
  else
    psql_super -d postgres -c "CREATE DATABASE $TEST_DB OWNER $DB_OWNER" >/dev/null 2>&1 || \
    psql_super -d postgres -c "CREATE DATABASE $TEST_DB" >/dev/null
  fi
  echo ">> installing extensions"
  for ext in postgis postgis_topology postgis_tiger_geocoder fuzzystrmatch pg_trgm pgcrypto dblink postgres_fdw; do
    psql_super -d "$TEST_DB" -c "CREATE EXTENSION IF NOT EXISTS \"$ext\" CASCADE;" >/dev/null 2>&1 \
      && echo "   ok $ext" || echo "   FAIL $ext"
  done
}

cmd_migrate() {
  echo ">> alembic upgrade head against $TEST_DB"
  run_in_image 'alembic upgrade head 2>&1 | tail -6; echo "--- current ---"; alembic current 2>&1 | tail -1'
}

cmd_seed() {
  echo ">> seeding edge-case fixtures into $TEST_DB"
  run_in_image 'python -m scripts.seed.seed_test_fixtures'
}

cmd_up() {
  echo ">> (re)starting $APP_CONTAINER on http://127.0.0.1:$HOST_PORT"
  docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$APP_CONTAINER" --network "$NETWORK" --env-file .env \
    -e DATABASE_URL="$TEST_DB_URL" \
    -e REDIS_URL="$REDIS_URL" \
    -e SESSION_REDIS_URL="$REDIS_URL" \
    -e CELERY_BROKER_URL="$REDIS_URL" \
    -e CELERY_RESULT_BACKEND="redis://:${REDIS_PW}@redis-local:6379/6" \
    -e APP_ENV=development \
    -e SERVER_NAME=dotmac-sub-test \
    -e GLITCHTIP_ENABLED=false \
    -e WEB_CONCURRENCY=1 \
    -p "127.0.0.1:${HOST_PORT}:8001" \
    -v "$REPO/app:/app/app" \
    -v "$REPO/templates:/app/templates" \
    -v "$REPO/static:/app/static" \
    -v "$REPO/scripts:/app/scripts" \
    --entrypoint sh "$IMAGE" \
    -c 'uvicorn app.main:app --host 0.0.0.0 --port 8001 --no-access-log --workers 1' >/dev/null
  echo -n "   waiting for startup"
  for i in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${HOST_PORT}/auth/login" 2>/dev/null || true)
    if [ "$code" = "200" ]; then echo " — READY (HTTP 200)"; return 0; fi
    echo -n "."; sleep 5
  done
  echo " — TIMED OUT; check: docker logs $APP_CONTAINER"
  return 1
}

cmd_down()   { docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 && echo "stopped $APP_CONTAINER" || echo "not running"; }
cmd_logs()   { docker logs -f "$APP_CONTAINER"; }
cmd_status() {
  echo "app:  $(docker ps --filter name=$APP_CONTAINER --format '{{.Status}}  {{.Ports}}' || echo 'down')"
  echo "db:   $TEST_DB on $PG_CONTAINER"
  echo "url:  http://127.0.0.1:$HOST_PORT"
}
cmd_psql()   { shift || true; if [ $# -gt 0 ]; then psql_super -d "$TEST_DB" -c "$*"; else psql_super -d "$TEST_DB"; fi; }
cmd_drop()   { docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true; psql_super -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB WITH (FORCE);"; echo "dropped $TEST_DB"; }

case "${1:-}" in
  create)    cmd_create ;;
  migrate)   cmd_migrate ;;
  seed)      cmd_seed ;;
  up)        cmd_up ;;
  down)      cmd_down ;;
  logs)      cmd_logs ;;
  psql)      cmd_psql "$@" ;;
  status)    cmd_status ;;
  bootstrap) cmd_create; cmd_migrate; cmd_seed; cmd_up ;;
  reset)     cmd_drop; cmd_create; cmd_migrate; cmd_seed; cmd_up ;;
  *) sed -n '2,40p' "$0"; exit 1 ;;
esac
