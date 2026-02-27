# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [2026-02-27]

### Security
- [Security] Upgrade jinja2 from 3.1.4 to 3.1.6 to fix CVE-2024-56201 and CVE-2024-56326 (sandbox escape via `|attr` filter chains and `__init_subclass__`) (PR #1)
- [Security] Upgrade cryptography from 42.0.8 to >=44.0.1 to fix CVE-2024-12797 (OpenSSL X.509 certificate verification bypass) (PR #2)
- [Security] Migrate JWT library from python-jose (CVE-2024-33663, CVE-2024-33664, abandoned 2022) to authlib; explicit algorithm enforcement prevents algorithm-confusion attacks (PR #8)
- [Security] Add `require_permission('auth:admin')` to all 21 previously unauthenticated endpoints in `app/api/auth.py` (user-credentials, MFA, sessions, API keys) (PR #7)
- [Security] `CREDENTIAL_ENCRYPTION_KEY` env var now required at startup; missing key raises `RuntimeError` instead of silently storing credentials in plaintext (PR #10)
- [Security] Add `require_permission('system:settings:write')` to `PUT /settings/gis/{key}` and `PUT /settings/geocoding/{key}` endpoints that lacked auth dependencies (PR #13)
- [Security] Fix path traversal in `delete_avatar()`: replace `str.replace()` prefix stripping with `Path.resolve()` + `relative_to()` containment check (PR #12)
- [Security] Add RADIUS table name allowlist validation in `enforcement.py`; `validate_radius_table()` guards all f-string SQL interpolations against injection (PR #14)
- [Security] Add RADIUS table name allowlist validation in `radius.py`; `ALLOWED_RADIUS_TABLES` frozenset with `validate_radius_table()` wrapper on all 8 interpolation sites (PR #16)
- [Security] Validate `bao://` mount and path components in `resolve_openbao_ref()` with regex allowlist; reject `..`, URL-encoded traversal sequences, and disallowed characters (PR #15)
- [Security] Fix path traversal in `stream_file()`: legacy local path from DB now validated with `Path.resolve().relative_to(base_upload_dir)` before opening (PR #18)
- [Security] Fix path traversal in export download endpoint: `file_path` from job record validated against `EXPORT_JOBS_BASE_DIR` with `Path.resolve().relative_to()` (PR #17)
- [Security] Block SSRF in webhook delivery task: enforce HTTPS scheme and reject RFC 1918 / loopback / link-local addresses before POSTing to `endpoint.url` (PR #20)
- [Security] Fix audit auth scope bypass: `require_audit_auth()` now enforces `audit:read` or `audit:*` scope on API keys, not just on JWT tokens (PR #21)
- [Security] Sanitize legal document content with `nh3` before DB storage to prevent stored XSS via `| safe` render in public legal pages (PR #22)
- [Security] Sanitize contract template HTML with `nh3` in `get_contract_context()` to prevent stored XSS via `| safe` in customer contract signing page (PR #19)
- [Security] Protect `/metrics` Prometheus endpoint with optional bearer token via `METRICS_AUTH_TOKEN` config; backward-compatible (unset = no auth) (PR #23)

### Changed
- [Changed] Upgrade OpenTelemetry from 1.26.0 to 1.39.1 and instrumentation packages from 0.47b0 (beta) to stable 0.60b1 (PR #6)
- [Changed] Upgrade fastapi to >=0.115.0 and uvicorn to >=0.34.0 for security-relevant Starlette fixes and request validation improvements (commit c10d3bf)

### Fixed
- [Fixed] Regenerate `poetry.lock` after pyproject.toml dependency upgrades to resolve CI lock-file staleness failure (PR #9)
