#!/usr/bin/env bash
# Create the pgBackRest repository directories and render its encryption secret.
# The repository cipher passphrase is created once in OpenBao and is never
# rotated automatically: old backups require the exact key they were written with.
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

SECRET_DIR="${PGBACKREST_SECRET_DIR:-/etc/dotmac/pgbackrest}"
SECRET_FILE="${SECRET_DIR}/secret.conf"
REPO_DIR="${PGBACKREST_REPO_DIR:-/var/backups/pgbackrest}"
SPOOL_DIR="${PGBACKREST_SPOOL_DIR:-/var/lib/dotmac-pgbackrest-spool}"
LOG_DIR="${PGBACKREST_LOG_DIR:-/var/log/dotmac_sub/pgbackrest}"
RESTORE_DIR="${PGBACKREST_RESTORE_VERIFY_DIR:-/var/lib/dotmac-pgbackrest-restore-verify}"
POSTGRES_UID="${PGBACKREST_POSTGRES_UID:-70}"
POSTGRES_GID="${PGBACKREST_POSTGRES_GID:-70}"
BAO_SECRET_PATH="${PGBACKREST_BAO_SECRET_PATH:-secret/backups/postgres}"
BAO_SECRET_FIELD="${PGBACKREST_BAO_SECRET_FIELD:-repo1_cipher_pass}"

export BAO_ADDR="${BAO_ADDR:-${OPENBAO_ADDR:-}}"
export BAO_TOKEN="${BAO_TOKEN:-${OPENBAO_TOKEN:-}}"

for command_name in bao install openssl; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done
if [[ -z "${BAO_ADDR}" || -z "${BAO_TOKEN}" ]]; then
  echo "OPENBAO_ADDR and OPENBAO_TOKEN (or BAO_ADDR/BAO_TOKEN) are required" >&2
  exit 1
fi

cipher_pass="$(bao kv get -field="${BAO_SECRET_FIELD}" "${BAO_SECRET_PATH}" 2>/dev/null || true)"
if [[ -z "${cipher_pass}" ]]; then
  cipher_pass="${PGBACKREST_CIPHER_PASS_SEED:-$(openssl rand -base64 48 | tr -d '\n')}"
  if [[ -z "${cipher_pass}" ]]; then
    echo "Could not generate pgBackRest repository cipher passphrase" >&2
    exit 1
  fi
  printf '%s' "${cipher_pass}" | bao kv put "${BAO_SECRET_PATH}" "${BAO_SECRET_FIELD}=-" >/dev/null
  echo "Created pgBackRest repository cipher passphrase in OpenBao at ${BAO_SECRET_PATH}"
else
  echo "Using existing pgBackRest repository cipher passphrase from ${BAO_SECRET_PATH}"
fi

install -d -m 0750 -o root -g "${POSTGRES_GID}" "${SECRET_DIR}"
install -d -m 0750 -o "${POSTGRES_UID}" -g "${POSTGRES_GID}" \
  "${REPO_DIR}" "${SPOOL_DIR}" "${LOG_DIR}" "${RESTORE_DIR}"
touch "${RESTORE_DIR}/.dotmac-pgbackrest-restore-target"
chown "${POSTGRES_UID}:${POSTGRES_GID}" "${RESTORE_DIR}/.dotmac-pgbackrest-restore-target"
chmod 0600 "${RESTORE_DIR}/.dotmac-pgbackrest-restore-target"

tmp_secret="$(mktemp)"
trap 'rm -f "${tmp_secret}"; unset cipher_pass' EXIT
chmod 0600 "${tmp_secret}"
printf '[global]\nrepo1-cipher-pass=%s\n' "${cipher_pass}" > "${tmp_secret}"
install -m 0600 -o "${POSTGRES_UID}" -g "${POSTGRES_GID}" "${tmp_secret}" "${SECRET_FILE}"

echo "Rendered ${SECRET_FILE}; repository=${REPO_DIR}; spool=${SPOOL_DIR}"
