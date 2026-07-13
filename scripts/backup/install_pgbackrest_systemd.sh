#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DEFAULT_FILE="${PGBACKREST_SYSTEMD_DEFAULT_FILE:-/etc/default/dotmac-pgbackrest}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root so systemd units can be installed" >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }

install -d -m 0755 "${SYSTEMD_DIR}" "$(dirname "${DEFAULT_FILE}")"
printf 'DOTMAC_SUB_DIR=%q\n' "${ROOT_DIR}" > "${DEFAULT_FILE}"
chmod 0644 "${DEFAULT_FILE}"
install -m 0644 "${ROOT_DIR}"/deploy/systemd/dotmac-pgbackrest-* "${SYSTEMD_DIR}/"
systemctl daemon-reload
systemctl enable --now \
  dotmac-pgbackrest-incr.timer \
  dotmac-pgbackrest-diff.timer \
  dotmac-pgbackrest-full.timer \
  dotmac-pgbackrest-health.timer \
  dotmac-pgbackrest-restore-verify.timer
systemctl list-timers 'dotmac-pgbackrest-*' --no-pager
