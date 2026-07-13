#!/usr/bin/env bash
# Disposable Docker smoke test for WAL archive, backup, metadata, and restore.
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
IMAGE="${PGBACKREST_TEST_IMAGE:-dotmac/postgis-pgbackrest:test}"
TEST_ID="$$"
PRIMARY="dotmac_pgbackrest_smoke_${TEST_ID}"
RESTORED="dotmac_pgbackrest_restore_smoke_${TEST_ID}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dotmac-pgbackrest-smoke.XXXXXX")"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root so bind-mounted PostgreSQL directories can be owned by uid 70" >&2
  exit 1
fi
cleanup() {
  docker rm -f "${RESTORED}" "${PRIMARY}" >/dev/null 2>&1 || true
  rm -rf "${TEST_ROOT}"
}
trap cleanup EXIT

mkdir -p "${TEST_ROOT}"/{data,repo,spool,log,restore,conf.d}
chown -R 70:70 "${TEST_ROOT}"
printf '[global]\nrepo1-cipher-pass=smoke-test-only-not-a-production-secret\n' > "${TEST_ROOT}/conf.d/secret.conf"
chown 70:70 "${TEST_ROOT}/conf.d/secret.conf"
chmod 0600 "${TEST_ROOT}/conf.d/secret.conf"

docker run -d --rm \
  --name "${PRIMARY}" \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=smoke-test \
  -e POSTGRES_DB=dotmac_backup_test \
  -v "${TEST_ROOT}/data:/var/lib/postgresql/data" \
  -v "${TEST_ROOT}/repo:/var/lib/pgbackrest" \
  -v "${TEST_ROOT}/spool:/var/spool/pgbackrest" \
  -v "${TEST_ROOT}/log:/var/log/pgbackrest" \
  -v "${TEST_ROOT}/restore:/var/lib/postgresql/restore-verify" \
  -v "${ROOT_DIR}/config/pgbackrest/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro" \
  -v "${TEST_ROOT}/conf.d:/etc/pgbackrest/conf.d:ro" \
  "${IMAGE}" \
  postgres \
    -c wal_level=replica \
    -c archive_mode=on \
    -c "archive_command=pgbackrest --stanza=dotmac-sub archive-push %p" \
    -c archive_timeout=10 \
  >/dev/null

deadline=$((SECONDS + 120))
until docker exec "${PRIMARY}" pg_isready -U postgres >/dev/null 2>&1; do
  if ((SECONDS >= deadline)); then
    docker logs "${PRIMARY}" >&2 || true
    echo "Smoke-test primary did not become ready" >&2
    exit 1
  fi
  sleep 2
done

docker exec --user postgres "${PRIMARY}" pgbackrest --stanza=dotmac-sub stanza-create
docker exec --user postgres "${PRIMARY}" pgbackrest --stanza=dotmac-sub check
docker exec "${PRIMARY}" psql -X -v ON_ERROR_STOP=1 -U postgres -d dotmac_backup_test -c \
  "CREATE TABLE backup_smoke (id integer PRIMARY KEY); INSERT INTO backup_smoke VALUES (1), (2);" \
  >/dev/null
docker exec --user postgres "${PRIMARY}" \
  pgbackrest --stanza=dotmac-sub --type=full --start-fast backup
docker exec "${PRIMARY}" psql -X -v ON_ERROR_STOP=1 -U postgres -d dotmac_backup_test -c \
  "INSERT INTO backup_smoke VALUES (3);" >/dev/null
docker exec --user postgres "${PRIMARY}" \
  pgbackrest --stanza=dotmac-sub --type=incr --start-fast backup

docker exec --user postgres "${PRIMARY}" pgbackrest --stanza=dotmac-sub --output=json info \
  | python3 "${ROOT_DIR}/scripts/backup/pgbackrest_info.py" --stanza dotmac-sub --max-age-seconds 600

docker exec --user postgres "${PRIMARY}" \
  pgbackrest --stanza=dotmac-sub \
  --pg1-path=/var/lib/postgresql/restore-verify \
  --type=immediate --target-action=promote restore

docker run -d --rm \
  --name "${RESTORED}" \
  --network none \
  -v "${TEST_ROOT}/restore:/var/lib/postgresql/data" \
  -v "${TEST_ROOT}/repo:/var/lib/pgbackrest:ro" \
  -v "${TEST_ROOT}/spool:/var/spool/pgbackrest" \
  -v "${ROOT_DIR}/config/pgbackrest/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro" \
  -v "${TEST_ROOT}/conf.d:/etc/pgbackrest/conf.d:ro" \
  "${IMAGE}" \
  postgres -c archive_mode=off -c listen_addresses= -c unix_socket_directories=/tmp -c port=55432 \
  >/dev/null

deadline=$((SECONDS + 120))
until docker exec "${RESTORED}" pg_isready -h /tmp -p 55432 -U postgres >/dev/null 2>&1; do
  if ((SECONDS >= deadline)); then
    docker logs "${RESTORED}" >&2 || true
    echo "Smoke-test restored database did not become ready" >&2
    exit 1
  fi
  sleep 2
done

row_count="$(
  docker exec "${RESTORED}" psql -X -v ON_ERROR_STOP=1 -h /tmp -p 55432 \
    -U postgres -d dotmac_backup_test -At -c "SELECT count(*) FROM backup_smoke"
)"
if [[ "${row_count}" != "3" ]]; then
  echo "Restore verification expected 3 rows, found ${row_count}" >&2
  exit 1
fi
echo "pgbackrest_smoke_ok restored_rows=${row_count}"
