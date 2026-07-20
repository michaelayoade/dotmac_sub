#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
POETRY_BIN="${POETRY_BIN:-poetry}"
PYTHON_BIN="${CRM_TICKET_IMPORT_PYTHON_BIN:-}"
STATE_FILE="${CRM_TICKET_IMPORT_STATE_FILE:-${ROOT_DIR}/var/phase1-ticket-import-state.json}"
OVERRIDES_CSV="${CRM_TICKET_IMPORT_OVERRIDES_CSV:-}"
OVERLAP_SECONDS="${CRM_TICKET_IMPORT_OVERLAP_SECONDS:-600}"
EXCLUDE_TITLE_REGEX="${CRM_TICKET_IMPORT_EXCLUDE_TITLE_REGEX:-}"

if [[ -z "${SUB_DATABASE_URL:-}" ]]; then
  echo "SUB_DATABASE_URL is required" >&2
  exit 2
fi

if [[ -z "${CRM_DATABASE_URL:-}" ]]; then
  echo "CRM_DATABASE_URL is required" >&2
  exit 2
fi

mkdir -p "$(dirname "${STATE_FILE}")"

args=(
  "${ROOT_DIR}/scripts/migration/import_crm_tickets_phase1.py"
  --apply
  --state-file "${STATE_FILE}"
  --state-overlap-seconds "${OVERLAP_SECONDS}"
  --no-allow-unmapped-closed
)

if [[ -n "${OVERRIDES_CSV}" ]]; then
  args+=(--overrides-csv "${OVERRIDES_CSV}")
fi

if [[ -n "${EXCLUDE_TITLE_REGEX}" ]]; then
  args+=(--exclude-title-regex "${EXCLUDE_TITLE_REGEX}")
fi

cd "${ROOT_DIR}"
if [[ -n "${PYTHON_BIN}" ]]; then
  exec "${PYTHON_BIN}" "${args[@]}"
fi

exec "${POETRY_BIN}" run python "${args[@]}"
