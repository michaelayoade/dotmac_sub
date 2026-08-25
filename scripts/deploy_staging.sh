#!/usr/bin/env bash
# Staging-only deployment adapter.
#
# The staging database is non-authoritative and shares its host disk with the
# application stack. A full local pg_dump before every staging deployment can
# therefore starve the running services of I/O without protecting production.
# This adapter proves the exact staging host contract before opting out of that
# local dump. Production and every other environment continue to call
# scripts/deploy.sh directly, where backups remain enabled by default.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

die() {
  echo "Staging deploy refused: $*" >&2
  exit 1
}

require_exact_env_line() {
  local expected="$1"
  grep -Fqx "${expected}" "${ENV_FILE}" || die "${ENV_FILE} must contain ${expected}"
}

[[ -f "${ENV_FILE}" ]] || die "missing ${ENV_FILE}"
require_exact_env_line "APP_ENV=staging"
require_exact_env_line "SERVER_NAME=dotmac-sub-staging"
require_exact_env_line "HEALTH_URL=http://10.120.121.20:8001/health"

cd "${ROOT_DIR}"

# Serialize every heavy Sub staging mutation behind a host-wide lock. Other
# staging repositories and the nightly database-sync owner adopt this same
# lock path so their deploy/restore work cannot overlap on Seabone's disk.
STAGING_HOST_LOCK_FILE="${STAGING_HOST_LOCK_FILE:-/var/lock/dotmac_staging_heavy.lock}"
command -v flock >/dev/null || die "flock(1) is required for host-wide admission"
if ! { exec 8>"${STAGING_HOST_LOCK_FILE}"; } 2>/dev/null; then
  die "cannot open host-wide lock ${STAGING_HOST_LOCK_FILE}"
fi
if ! flock -n 8; then
  die "another staging deploy, restore, or maintenance operation holds ${STAGING_HOST_LOCK_FILE}"
fi

# The typed policy consumes bounded host observations and fails before any
# image pull, migration, or container recreation when the host is unsafe.
python3 "${ROOT_DIR}/scripts/staging_host_admission.py"

export SKIP_BACKUP=1
export REQUIRE_PROXY_HANDOFF=0
exec bash "${ROOT_DIR}/scripts/deploy.sh" "$@"
