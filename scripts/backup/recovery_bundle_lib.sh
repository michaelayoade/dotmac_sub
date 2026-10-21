#!/usr/bin/env bash
# Shared recovery-bundle primitives — the SINGLE SOURCE for backup dump flags.
#
# Why this file exists (docs/designs/SUB_DEPLOYMENT_EXECUTOR_CENSUS.md, "The
# backup defect"): both backup call sites ran `pg_dump -d`, which never emits
# role definitions, and db_backup.sh additionally passed
# `--no-owner --no-privileges`, stripping ownership and every GRANT. A restore
# therefore produced a database that looked complete while its isolation
# model (RLS + role grants/revocations) was absent.
#
# Facility search verdict: Starter DOES ship a real facility —
# dotmac-deployment-foundation `PostgresRecoveryBundle.v1`
# (packages/dotmac-deployment-foundation/src/dotmac_deployment_foundation/
# recovery.py): role-closure derivation, manifest schema, restore ordering,
# `refuse_identity_stripping`. But (a) it deliberately runs no pg_dump — host
# scripts must still produce artifacts — and (b) Sub does not depend on
# dotmac-deployment-foundation yet (pyproject.toml names neither Foundation
# nor Control; census: pinning waits on a2 being published). So this lib is
# the minimal host-side capture aligned with that facility, to be replaced by
# the facility's own capture when the pin lands:
#   * identity-stripping flags are refused (facility IDENTITY_STRIPPING_ARGS);
#   * role capture carries NO password material (`--no-role-passwords`,
#     facility REQUIRED_ROLE_CAPTURE_ARG) — `--globals-only` alone would
#     write SCRAM verifiers into backup storage; login material is installed
#     at restore time from the approved secret source, never from a bundle.
#
# Globals flag decision: `pg_dumpall --globals-only --no-role-passwords`
# rather than `--roles-only`: globals-only also captures tablespace
# definitions and role memberships, so no separate tablespace artifact can
# drift out of sync. It does NOT capture database-level ACLs or settings on
# PostgreSQL >= 11 (GRANT/REVOKE CONNECT ON DATABASE, ALTER DATABASE ... SET,
# ALTER ROLE ... IN DATABASE ... SET); `pg_dump -d` without --create does not
# either. Those need a separate db-properties artifact (known follow-up).
# The restore drill applies globals with error-stop off and then ASSERTS the
# isolation model, so benign "already exists" noise cannot mask a real
# absence.
#
# NEVER apply a globals file to a cluster whose roles already exist: it
# re-applies backup-time role attributes and memberships (see
# docs/RECOVERY_BUNDLE.md).
#
# Sourced, not executed. Callers keep their own docker-exec pattern and
# credential handling; every flag comes from here so the defect class cannot
# silently reappear in a third script.

# Flags for the per-database dump. Deliberately NO --no-owner/--no-privileges:
# ownership and ACLs are the evidence the bundle exists to carry.
RECOVERY_PG_DUMP_FLAGS=""

# Flags for the cluster-globals capture (see flag decision above).
RECOVERY_GLOBALS_FLAGS="--globals-only --no-role-passwords"

# Dump flags that delete bundle evidence at capture time. Mirrors the
# facility's IDENTITY_STRIPPING_ARGS; keep the two in sync.
RECOVERY_FORBIDDEN_DUMP_FLAGS="--no-owner -O --no-privileges -x --no-acl --no-security-labels"

# Distinct exit code: a backup that failed its globals step must never be
# readable as an ordinary failure, let alone as success.
RECOVERY_GLOBALS_FAILURE_EXIT=3

# Refuse a proposed dump-flag string that strips identity evidence.
recovery_refuse_identity_stripping() {
  local flags="$1" forbidden
  for forbidden in ${RECOVERY_FORBIDDEN_DUMP_FLAGS}; do
    case " ${flags} " in
      *" ${forbidden} "*|*" ${forbidden}="*)
        echo "REFUSING backup: dump flag '${forbidden}' strips ownership/privileges evidence" >&2
        return 1
        ;;
    esac
  done
  return 0
}

# Fail if a (decompressed-on-stdin or plain) globals file contains password
# verifier material. Input is a .sql.gz path.
recovery_assert_no_password_material() {
  local globals_gz="$1"
  if gzip -dc "${globals_gz}" | grep -Eq "PASSWORD '(SCRAM-SHA-256\\\$|md5[0-9a-f]{32})"; then
    echo "REFUSING backup: globals artifact contains password verifier material" >&2
    return 1
  fi
  return 0
}

recovery_sha256() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${file}" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${file}" | cut -d' ' -f1
  else
    echo "REFUSING backup: no sha256 tool (sha256sum/shasum) available" >&2
    return 1
  fi
}

recovery_file_bytes() {
  # GNU stat first, BSD fallback; wc is the last resort.
  stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c < "$1" | tr -d ' '
}

# recovery_write_manifest <manifest_path> <database> <globals_file> <dump_file> <pg_version> <git_sha>
# Artifact paths in the manifest are basenames so a bundle stays valid when
# moved (e.g. downloaded from rclone) before a restore drill.
recovery_write_manifest() {
  local manifest_path="$1" database="$2" globals_file="$3" dump_file="$4"
  local pg_version="$5" git_sha="$6"
  local created globals_sha dump_sha globals_bytes dump_bytes
  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  globals_sha="$(recovery_sha256 "${globals_file}")" || return 1
  dump_sha="$(recovery_sha256 "${dump_file}")" || return 1
  globals_bytes="$(recovery_file_bytes "${globals_file}")"
  dump_bytes="$(recovery_file_bytes "${dump_file}")"
  # pg_version/git_sha are operator/tool-controlled single tokens; strip
  # anything that could break the JSON string.
  pg_version="$(printf '%s' "${pg_version}" | tr -cd 'A-Za-z0-9 ._-')"
  git_sha="$(printf '%s' "${git_sha}" | tr -cd 'A-Za-z0-9_-')"
  {
    printf '{\n'
    printf '  "schema": "dotmac_sub.recovery_bundle.v1",\n'
    printf '  "facility_target": "PostgresRecoveryBundle.v1 (dotmac-deployment-foundation; adoption blocked on pin)",\n'
    printf '  "created_at": "%s",\n' "${created}"
    printf '  "database": "%s",\n' "${database}"
    printf '  "pg_dump_version": "%s",\n' "${pg_version}"
    printf '  "git_sha": "%s",\n' "${git_sha}"
    printf '  "restore_order": ["globals", "database_dump", "verify"],\n'
    printf '  "login_material": "absent_by_design: install role passwords from the approved secret source after globals",\n'
    printf '  "artifacts": [\n'
    printf '    {"role": "globals", "file": "%s", "sha256": "%s", "bytes": %s, "command": "pg_dumpall %s"},\n' \
      "$(basename "${globals_file}")" "${globals_sha}" "${globals_bytes}" "${RECOVERY_GLOBALS_FLAGS}"
    printf '    {"role": "database_dump", "file": "%s", "sha256": "%s", "bytes": %s, "command": "pg_dump -d %s %s"}\n' \
      "$(basename "${dump_file}")" "${dump_sha}" "${dump_bytes}" "${database}" "${RECOVERY_PG_DUMP_FLAGS}"
    printf '  ]\n'
    printf '}\n'
  } > "${manifest_path}"
}

# Mark a failed bundle attempt and exit distinctly. The marker names the
# would-have-been dump so an operator (or retention glob) can see the hole;
# no dump artifact is left behind to be mistaken for a restorable backup.
recovery_fail_globals() {
  local dump_path="$1" reason="$2"
  local marker="${dump_path%.sql.gz}.FAILED"
  printf 'globals step failed: %s\nat: %s\nno restorable bundle was produced\n' \
    "${reason}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${marker}" 2>/dev/null || true
  echo "BACKUP FAILED (globals): ${reason}; marker: ${marker}" >&2
  exit "${RECOVERY_GLOBALS_FAILURE_EXIT}"
}
