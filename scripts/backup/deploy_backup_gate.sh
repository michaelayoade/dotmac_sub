#!/usr/bin/env bash
# Prove recoverability before migrations without creating backup load during deploy.
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

MODE="${BACKUP_MODE:-auto}"
DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"
LOGICAL_BACKUP_DIR="${DB_BACKUP_DIR:-/var/backups/dotmac_sub}"
LOGICAL_BACKUP_BASENAME="${DB_BACKUP_BASENAME:-dotmac_sub}"
LOGICAL_MAX_AGE_SECONDS="${LOGICAL_BACKUP_MAX_AGE_SECONDS:-90000}"
LOGICAL_MIN_BYTES="${LOGICAL_BACKUP_MIN_BYTES:-1048576}"

if [[ "${SKIP_BACKUP_CHECK:-0}" == "1" || "${SKIP_BACKUP:-0}" == "1" ]]; then
  echo "WARNING: backup recoverability gate skipped by emergency override" >&2
  exit 0
fi

pgbackrest_available() {
  docker exec --user postgres "${DB_CONTAINER}" pgbackrest version >/dev/null 2>&1
}

logical_gate() {
  local latest_backup modified_at now age size
  latest_backup="$(
    find "${LOGICAL_BACKUP_DIR}" -maxdepth 1 -type f \
      -name "${LOGICAL_BACKUP_BASENAME}_*.sql.gz" ! -name '*.partial' \
      -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true
  )"
  if [[ -z "${latest_backup}" || ! -f "${latest_backup}" ]]; then
    echo "BACKUP HEALTH FAILURE: no completed logical backup found in ${LOGICAL_BACKUP_DIR}" >&2
    return 1
  fi
  modified_at="$(stat -c %Y "${latest_backup}")"
  now="$(date +%s)"
  age=$((now - modified_at))
  size="$(stat -c %s "${latest_backup}")"
  if ((age > LOGICAL_MAX_AGE_SECONDS)); then
    echo "BACKUP HEALTH FAILURE: ${latest_backup} is ${age}s old (max ${LOGICAL_MAX_AGE_SECONDS}s)" >&2
    return 1
  fi
  if ((size < LOGICAL_MIN_BYTES)); then
    echo "BACKUP HEALTH FAILURE: ${latest_backup} is only ${size} bytes" >&2
    return 1
  fi
  # Full CRC verification reads the backup file but does not query PostgreSQL.
  # Run at low priority so the app remains responsive during the transitional gate.
  nice -n 15 ionice -c 2 -n 7 gzip -t "${latest_backup}"
  echo "backup_ok mode=logical path=${latest_backup} age_seconds=${age} bytes=${size}"
}

case "${MODE}" in
  pgbackrest)
    pgbackrest_available || {
      echo "BACKUP HEALTH FAILURE: BACKUP_MODE=pgbackrest but pgBackRest is unavailable" >&2
      exit 1
    }
    exec bash "${ROOT_DIR}/scripts/backup/pgbackrest_health.sh" --gate
    ;;
  logical)
    logical_gate
    ;;
  auto)
    if pgbackrest_available; then
      exec bash "${ROOT_DIR}/scripts/backup/pgbackrest_health.sh" --gate
    fi
    echo "WARNING: pgBackRest is not initialized; using transitional logical-backup gate" >&2
    logical_gate
    ;;
  *)
    echo "Invalid BACKUP_MODE=${MODE}; expected pgbackrest, logical, or auto" >&2
    exit 2
    ;;
esac
