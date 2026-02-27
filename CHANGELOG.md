# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [2026-02-27]

### Security
- [Security] Fix IDOR in WireGuard peer config download: `download_peer_config()` and `download_mikrotik_script()` now verify peer ownership against the caller's subscriber_id; non-admin callers receive 403 for peers they do not own (PR #38)
- [Security] Change `plaintext_login=True` to `plaintext_login=False` for RouterOS API connections in `wireguard.py`, `web_vpn_servers.py`, `provisioning_adapters.py`, and `mikrotik_poller.py` to prevent passwords being sent in plaintext (PR #41)
- [Security] Change non-loopback `http://` defaults in `app/config.py` to `https://`; add startup warning log when any configured service URL uses `http://` to a non-loopback host (PR #36)
- [Security] Add inline rationale to all `# noqa: S608` suppressions in `radius.py` and `enforcement.py`: table names are validated against `ALLOWED_RADIUS_TABLES` allowlist, not user-supplied input (PR #37)
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
- [Security] Remove `ssl_verify=False` and `plaintext_login=True` from all 5 RouterOS connection sites in `wireguard.py`, `web_vpn_servers.py`, `provisioning_adapters.py`, and `mikrotik_poller.py`; enforce TLS verification (PR #11)
- [Security] Block SSRF in `_probe_embedded_url_health()` in `web_integrations.py`: resolve hostname and reject RFC 1918 / loopback / link-local addresses before calling httpx.get() (PR #24)
- [Security] Add `@limiter.limit('20/minute')` rate limit on `POST /auth/login` via slowapi to prevent credential brute-force attacks (PR #26)
- [Security] Change S3 `s3_access_key`/`s3_secret_key` config defaults from well-known MinIO credentials (`minioadmin`) to `None`; add guard raising `ValueError` when S3 is used without credentials configured (PR #25)
- [Security] Change MySQL password config default from empty string to `None`; add connection guard raising `ValueError` when MySQL is used without a password (PR #29)
- [Security] Move account lockout check before password verification in login flow to prevent timing oracle — correct password + locked account no longer returns a distinct HTTP 403 response (PR #30)
- [Security] Add minimum 32-character length check on JWT signing secret in `_jwt_secret()`; raises `HTTPException(500)` if `SECRET_KEY` is too short to be safe (PR #31)
- [Security] Block SSRF in SMS webhook delivery: enforce HTTPS scheme and reject RFC 1918 / loopback / link-local addresses before POSTing to `webhook_url` in `sms.py` (PR #32)
- [Security] Block SSRF in Nextcloud Talk `resolve_talk_client()`: enforce HTTPS scheme and reject RFC 1918 / loopback / link-local addresses for caller-supplied `base_url` (PR #33)
- [Security] Fix path traversal in OLT backup path validation in `web_network_olts.py`: replace `str.startswith()` check with `Path.resolve().relative_to()` containment check (PR #35)
- [Security] Fix open redirect in `_safe_next()`: reject protocol-relative URLs (`//evil.com`) and absolute URLs; only root-relative paths (starting with `/` but not `//`) are accepted as redirect targets in `web_auth.py` and `web_customer_auth.py` (PR #48)
- [Security] Fix path traversal in WireGuard config path: add `pattern='^[A-Za-z][A-Za-z0-9_-]{0,14}$'` constraint to `interface_name` in `WireGuardServerBase` and `WireGuardServerUpdate` schemas; add `Path.resolve().relative_to(WG_CONFIG_DIR)` containment check in `WireGuardSystemService.get_config_path()` (PR #49)

### Changed
- [Changed] Upgrade celery from 5.4.0 to >=5.5.0 (PR #40)
- [Changed] Upgrade redis from 5.0.4 to >=5.2.0 (PR #39)
- [Changed] Upgrade sqlalchemy from 2.0.31 to >=2.0.40 (PR #43)
- [Changed] Upgrade alembic from 1.13.2 to >=1.14.0 (PR #42)
- [Changed] Upgrade shapely from 2.0.4 to >=2.1.0; verified geoalchemy2 0.14.7 compatibility (PR #45)
- [Changed] Add explicit `urllib3>=2.0` dependency to mitigate CVE-2023-43804 and CVE-2023-45803 (PR #44)
- [Changed] Upgrade OpenTelemetry from 1.26.0 to 1.39.1 and instrumentation packages from 0.47b0 (beta) to stable 0.60b1 (PR #6)
- [Changed] Upgrade fastapi to >=0.115.0 and uvicorn to >=0.34.0 for security-relevant Starlette fixes and request validation improvements (commit c10d3bf)
- [Changed] Upgrade weasyprint from 61.2 to >=65.0 to address SSRF risks in older versions when rendering user-supplied HTML (PR #27)
- [Changed] Upgrade pydantic from 2.7.4 to >=2.11.0 (PR #28)
- [Changed] Upgrade httpx from 0.27.0 to >=0.28.0 (PR #34)

### Added
- [Added] Automated security tests in `tests/security/` covering SSRF guards (webhook delivery, SMS webhook), path traversal (file storage, export download), and login rate limiting (PR #47)

### Fixed
- [Fixed] Regenerate `poetry.lock` after pyproject.toml dependency upgrades to resolve CI lock-file staleness failure (PR #9)
