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

### Changed
- [Changed] Upgrade OpenTelemetry from 1.26.0 to 1.39.1 and instrumentation packages from 0.47b0 (beta) to stable 0.60b1 (PR #6)
- [Changed] Upgrade fastapi to >=0.115.0 and uvicorn to >=0.34.0 for security-relevant Starlette fixes and request validation improvements (commit c10d3bf)

### Fixed
- [Fixed] Regenerate `poetry.lock` after pyproject.toml dependency upgrades to resolve CI lock-file staleness failure (PR #9)
