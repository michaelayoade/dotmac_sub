# Seabone Memory — dotmac_sub

## Project Facts

### From CLAUDE.md
> # CLAUDE.md
> 
> This file provides context for Claude Code when working on this project.
> 
> ## Project Overview
> 
> **DotMac Sub** is a multi-tenant subscription management system for ISPs and fiber network operators. It handles subscriber lifecycle, catalog management, billing, network provisioning, and service orders.
> 
> ## Quick Commands
> 
> ```bash
> # Quality (or use: make check)
> make lint                        # ruff check app/
> make format                      # ruff format + fix
> make type-check                  # mypy app/
> make security                    # bandit security scan
> make check                       # All quality checks
> 
> # Testing (or use: make test)
> make test                        # Run test suite

### Stack Detection
- Build: pyproject.toml detected

## Known Patterns

### Security

#### Auth gaps (as of 2026-02-27)
- `app/api/auth.py` — ALL 21 endpoints (user-credentials, MFA, sessions, API keys) are unauthenticated. Needs `require_permission` on every decorator. This is the highest-priority security finding.
- `app/api/settings.py` — `PUT /settings/gis/{key}` and `PUT /settings/geocoding/{key}` lack auth dependencies.
- Good reference for correct auth pattern: `app/api/subscribers.py` and `app/api/rbac.py`.

#### Credential encryption
- `app/services/credential_crypto.py` — encryption is opt-in; missing `CREDENTIAL_ENCRYPTION_KEY` silently stores credentials in plaintext with `plain:` prefix instead of failing at startup.

#### RouterOS connections
- 5 locations use `ssl_verify=False` + `plaintext_login=True`: `app/services/wireguard.py` (lines 1307-1308, 1384-1385), `app/services/web_vpn_servers.py:642-643`, `app/services/provisioning_adapters.py:139`, `app/poller/mikrotik_poller.py:95`.

#### RADIUS SQL pattern
- `app/services/radius.py` and `app/services/enforcement.py` interpolate RADIUS table names into SQL f-strings (suppressed with `# noqa: S608`). Values come from config, not user input. Fix: allowlist validation on config load.

#### SSRF risks
- `app/services/secrets.py:39` — OpenBao URL constructed from user-supplied `bao://` reference without path validation.
- `app/services/web_integrations.py:268` — connector health probe has no RFC 1918 block.
- `app/services/sms.py:192` — SMS webhook URL from settings used without SSRF guard.

#### Path traversal risks
- `app/services/avatar.py:54` — `delete_avatar()` uses `str.replace()` for path extraction; use `Path.relative_to()`.
- `app/services/file_storage.py:360` — `legacy_local_path` from DB opened without directory containment check.
- `app/web/admin/system.py:623` — export `file_path` from job record served without containment check.
- `app/services/web_network_olts.py:190` — backup path uses `str.startswith()` containment check instead of `Path.relative_to()`.

#### Config defaults
- `app/config.py:47-49` — S3 defaults to `minioadmin`/`minioadmin` (well-known MinIO defaults).
- `app/config.py:30` — MySQL defaults to user `splynx`, empty password.

#### Additional SSRF vectors (found second pass)
- `app/tasks/webhooks.py:103` — webhook delivery POSTs to `endpoint.url` without RFC 1918 check.
- `app/services/nextcloud_talk.py:125` — `base_url` from request body passed directly to httpx without hostname validation.

#### Auth scope bypass
- `app/services/auth_dependencies.py:109` — `require_audit_auth()` grants any valid API key audit access regardless of its configured scopes; scope check only applies to JWT tokens.

#### Stored XSS via `| safe`
- `templates/public/legal/document.html:108` — `document.content | safe`, no HTML sanitization before storage.
- `templates/customer/contracts/sign.html:26` — `contract_html | safe`, service layer (contracts.py:180) does not sanitize despite comment claiming it does.

#### Metrics exposure
- `app/main.py:406` — `/metrics` Prometheus endpoint unauthenticated and public.

#### IDOR
- `app/api/wireguard.py:246` — WireGuard peer config download serves any peer's private key to any authenticated user.

#### Auth architecture clarification
- All API routers ARE protected — `main.py` applies `require_user_auth` at router registration via `_include_api_router()`. Individual route files may lack auth decorators but the router-level dependency in main.py covers them.
- Exception: `auth_flow_router` (login endpoint) and `wireguard_public_router` (token-based) are intentionally unauthenticated.

#### New findings (security cycle 8, 2026-02-28)
- `app/services/integration_hooks.py:417` — CRITICAL: `_execute_cli_hook()` uses `shell=True` with admin-controlled command string — OS RCE for any admin with integrations access. Fix: `shlex.split(command)` + `shell=False`.
- `app/api/scheduler.py:30` — HIGH: All 5 scheduler endpoints lack `require_permission`; any authenticated user can create tasks with arbitrary Celery `task_name` and enqueue them. Fix: add permission + task_name allowlist.
- `app/api/search.py:1` — HIGH: All 15 typeahead search endpoints lack `require_permission` (only `require_user_auth`).
- `app/api/analytics.py:1` — HIGH: All 8 analytics endpoints lack `require_permission`.
- `app/api/gis.py:368` — HIGH: `POST /gis/sync` (with `deactivate_missing` flag) lacks `require_permission`.
- `app/services/integration_hooks.py:398` — MEDIUM: HTTP integration hooks send outbound requests to `hook.url` without RFC 1918 SSRF guard.
- `app/api/nextcloud_talk.py:37,52,69` — MEDIUM: `detail=str(exc)` exposes exception internals to clients.
- `app/services/web_vendor_routes.py:372` — MEDIUM: `str(exc)` in JSONResponse exposed to vendor portal.
- `app/services/web_network_core_devices_views.py:45` — LOW: OLT name interpolated into PromQL without escaping.

#### Rate limiting gaps (as of 2026-02-27 third pass)
- Web form login endpoints are NOT rate limited: `/auth/login` (POST), `/portal/auth/login` (POST), `/reseller/auth/login` (POST). Only the API JSON login has slowapi.
- `/auth/forgot-password` POST has no rate limiting.
- Reference: `app/api/auth_flow.py:72` for correct slowapi pattern.

#### Open redirect in `_safe_next()`
- Both `app/services/web_customer_auth.py:24-28` and `app/services/web_auth.py:32-35` use `startswith("/")` which allows `//evil.com` protocol-relative URLs as open redirects.

#### WireGuard interface_name path traversal
- `app/services/wireguard_system.py:173` uses `WG_CONFIG_DIR / f"{server.interface_name}.conf"` without character validation.
- `app/schemas/wireguard.py:18-20` only validates length (1-32), not character set. Need `pattern='^[A-Za-z][A-Za-z0-9_-]{0,14}$'`.

#### Avatar upload content type
- `app/services/avatar.py:14-20` validates `file.content_type` from HTTP header only.
- `app/services/file_upload.py` has `MAGIC_BYTES` dict for proper validation — avatar service should use it.

### Quality

#### SQLAlchemy 1.x (as of 2026-02-27 quality cycle 2)
- 815+ `db.query()` / `session.query()` calls codebase-wide; project mandates `select()`.
- Worst offenders: `web_network_fiber.py` (40), `network_monitoring.py` (33), `collections/_core.py` (28).

#### Transaction ownership violation (as of 2026-02-27 quality cycle 2)
- 570 `db.commit()` in services vs 78 `db.flush()` — services own transactions instead of caller.
- Worst: `notification.py` (23), `billing/payments.py` (23), `billing/payments.py` (23), `subscriber.py` (18).

#### Monolithic functions (as of 2026-02-27 quality cycle 2)
- `scheduler_config.py:205` `build_beat_schedule()` — 513 lines.
- `billing/reporting.py:189` `get_dashboard_stats()` — 484 lines.
- `billing_automation.py:253` `run_invoice_cycle()` — 268 lines.
- `collections/_core.py:1106` + `:1346` — two `run()` methods 201/181 lines.

#### Async/sync route handler mixing
- `api/billing.py:943,957` — `async def` paystack/flutterwave handlers use sync SQLAlchemy Session.
- `web/admin/provisioning.py:231,250` — `async def` bulk-activate handlers use sync DB session.

#### Resource leaks
- `enforcement.py:748` — `create_engine()` never followed by `engine.dispose()` (connection pool leak).

#### Silent exception swallowing
- `usage.py:582` — `except Exception: pass` on usage charge posting.
- `web_billing_dunning.py:106` — `except Exception: continue` in bulk dunning.
- `web_catalog_settings.py:255-291` — 5× bulk-delete log failures at DEBUG only.

#### response_model=dict
- `api/nas.py` (5 endpoints) + `api/provisioning.py` (7 endpoints) use `response_model=dict`.

#### ORM relationships
- 148 `relationship()` calls missing `back_populates` across all model files.

#### API layer patterns (as of 2026-02-27 api cycle 3)
- `app/api/wireguard.py:49,149` — `list_servers` and `list_peers` call `to_read_schema(item, db)` per item in a list comprehension; `to_read_schema` makes DB queries (peer count, server name) causing N+1 patterns.
- `app/api/billing.py:76` — `billing_dashboard` has no `response_model` and imports the service module locally inside the function body instead of at module top.
- Endpoints without `response_model`: `fiber_plant.py` (4 endpoints), `nas.py` utility endpoints (`/vendors`, `/connection-types`, `/provisioning-actions`, `/backup-methods`, `/backups/{id}/content`, `/backups/compare`), `provisioning.py:68` (`/orders/stats`), `scheduler.py:58,63` (refresh/enqueue), `integrations.py:111` (refresh-schedule).
- Weak `response_model=dict` or `response_model=list[dict]`: `nextcloud_talk.py` (all 3 endpoints), `gis.py:207` (`/areas/{id}/contains-point`).
- Overall: ~85% of API endpoints have typed response models; gaps concentrated in utility/action/webhook endpoints.

#### Additional API gaps (as of 2026-02-27 api cycle 7)
- `app/api/imports.py:10` — `POST /imports/subscriber-custom-fields` has no `response_model` AND no `require_permission` — authorization gap on bulk import endpoint.
- `app/api/analytics.py:82` — `GET /analytics/kpis` returns unbounded `list[KPIReadout]` with no pagination; should use `ListResponse[KPIReadout]` with limit/offset.
- `app/api/nas.py:136,342,373` — three NAS list endpoints (backups, logs, device-logs) use `response_model=dict` instead of `ListResponse[NasConfigBackupRead]` / `ListResponse[ProvisioningLogRead]`.
- `app/api/nas.py:222,281` — templates list `response_model=dict`; preview endpoint no `response_model`.
- `app/api/nas.py:89` — device stats endpoint has no `response_model`.
- `app/api/gis.py:368` — `POST /gis/sync` has no `response_model`.
- `app/api/search.py:140` — `GET /search/global` is the only endpoint in search.py without `response_model`.
- `app/api/tables.py:78` — `GET /tables/{table_key}/data` extracts limit/offset from raw query params dict with no bounds validation; callers can pass `?limit=999999`.

#### Deps patterns (as of 2026-02-27 deps cycle 4)
- `app/services/nas.py:885,2128` — `import requests` inside two function bodies; `requests` is NOT in pyproject.toml (transitive only). Should use `httpx` (already declared).
- `pyproject.toml:43` — `aiosmtpd 1.4.6` declared but never imported anywhere in `app/`; all SMTP uses stdlib `smtplib`. Remove unused dependency.
- `app/api/bandwidth.py:13`, `app/web/request_parsing.py:7` — `import anyio` used directly but not declared; only available as transitive dep of FastAPI/starlette.
- `app/api/` and `app/schemas/` — only two `app/` subdirs without `__init__.py`. All others have it.
- `python-jose 3.3.0` — still in use (CVE-2024-33663/33664); `from jose import` still appears in `auth_flow.py:12` and `observability.py:6` despite fix-deps-003 being marked complete. Needs re-verification.
- `passlib 1.7.4` with `bcrypt 5.0.0` (in lock) — passlib unmaintained, uses removed `bcrypt.__about__` module; runtime warnings guaranteed. Still open (deps-004, no fix task created yet).

#### Missing logger in large service files (as of 2026-02-27 quality cycle 6)
- Only 99 of 231 service files (~43%) have `logger = logging.getLogger(__name__)`.
- Worst offenders: `nas.py` (2248 lines), `network_monitoring.py` (1333 lines), `auth_flow.py` (1044 lines), `notification.py` (899 lines), `subscriber.py` (802 lines) — all with zero logging.

#### ORM display-attribute mutation antipattern (as of 2026-02-27 quality cycle 6)
- `web_billing_payments.py:556-558` sets `display_number`, `display_method`, `narration` on ORM instances with `# type: ignore[attr-defined]`. Same in `web_network_speed_profiles.py:51,53`.
- Fix: use a typed DTO dataclass or TypedDict for enriched view data.

#### Hard-coded `limit=2000` silently truncates reports (quality cycle 6)
- `web_billing_overview.py:65,364` and `web_billing_payments.py:601` — AR aging + dashboard totals wrong for large deployments.

#### f-string logging antipattern (quality cycle 6)
- 94 `logger.info(f"...")` instances codebase-wide. Enable ruff rule G004 to auto-fix.

#### New monolithic functions (cycle 6 additions beyond cycle 2)
- `network_map.py:25` 323L, `web_network_core_devices_views.py:283` 211L, `web_billing_payments.py:443` 199L, `web_billing_overview.py:342` 198L, `web_subscriber_details.py:176` 192L.

#### New N+1 patterns (cycle 6)
- `bandwidth.py:452` — `db.get(Subscription)` + lazy-loads per metrics top-users row.
- `collections/_core.py:1143` — `db.get(Subscriber)` + 2 more queries per overdue account in dunning loop.

#### Redundant `import builtins` (quality cycle 6)
- `gis.py`, `network_monitoring.py`, `bandwidth.py`, `rbac.py` have BOTH `from __future__ import annotations` AND `import builtins` — the latter is redundant; plain `list[T]` works.

#### Resource leak in radius.py (quality cycle 9)
- `radius.py:283` and `radius.py:361` — `create_engine()` called without `engine.dispose()`, leaking connection pools on every RADIUS provisioning and NAS sync call (same bug as `enforcement.py:748`).

#### Celery tasks missing retry logic (quality cycle 9)
- 7 of 8 network-intensive tasks have no `autoretry_for` config: `snmp.py`, `olt_polling.py`, `olt_config_backup.py`, `nas.py`, `integrations.py`, `vpn.py`, `radius.py`. Only `webhooks.py` has correct retry pattern.

#### Blocking time.sleep in sync web handlers (quality cycle 9)
- `snmp_discovery.py:233` — `time.sleep(1)` for SNMP bandwidth sampling is called from `web_network_core_devices_forms.py` sync route handlers, blocking a Uvicorn worker thread for 1s per request.

#### Untyped `db` parameters (quality cycle 9)
- 51 service functions across 12+ files (including `web_network_cpes.py`, `web_billing_dunning.py`, `settings_spec.py`, `web_integrations.py`) declare `db` without `db: Session` annotation.

#### Missing `__init__.py` in api/ and schemas/ (quality cycle 9)
- `app/api/` and `app/schemas/` are the only two `app/` subdirs without `__init__.py`; all others have it.

#### Pagination gaps in API list endpoints (api cycle 10)
- `app/api/wireguard.py:37,130` — `list_servers` and `list_peers` return bare `list[X]` (not `ListResponse[X]`) despite having limit/offset params; clients cannot get total counts.
- `app/api/wireguard.py:331` — `list_peer_connection_logs` builds Pydantic models in the route handler (thin-wrapper violation) and returns bare list.
- `app/api/gis.py:103,122,223` — spatial query endpoints (`find_nearby_locations`, `find_locations_in_area`, `find_areas_containing_point`) have no `limit` parameter, allowing unbounded result sets.
- `app/api/nas.py:282` — `preview_template` has no `response_model` and constructs raw dict in route handler; needs `TemplatePreviewResponse` schema.
- `app/api/provisioning.py` and `app/api/nas.py` — use literal `status_code=201`/`204` (19 occurrences) instead of `status.HTTP_201_CREATED`/`HTTP_204_NO_CONTENT`; inconsistent with all other API files.

#### New deps findings (cycle 11, 2026-02-28)
- `pysnmp` imported (lazy, try/except ImportError) in `olt_config_backup.py:33` and `web_network_olts.py:278` but NOT in pyproject.toml and NOT installed — SNMP OLT backup silently broken everywhere.
- `asyncio.get_event_loop()` deprecated (Python 3.10+) used 5× in `mikrotik_poller.py` (lines 87, 117, 131, 155, 454); replace with `asyncio.get_running_loop()`.
- `stubs/requests/__init__.pyi` only covers `post()` but `nas.py:905,915` calls `requests.get()` — mypy silently ignores untyped calls.
- `shapely 2.0.4` declared in pyproject.toml but never imported in `app/`; geoalchemy2's transitive dep, not a direct dep — safe to remove.
- `boto3` in pyproject.toml but no `boto3-stubs[s3]` in dev deps — all `object_storage.py` boto3 calls are untyped.
- `python-jose` CVE fix (fix-deps-003) marked complete but `from jose import` still present in `auth_flow.py:12` and `observability.py:6`.
- `passlib` (fix-deps-004) never appeared in Already Fixed — still open at `auth_flow.py:13`.

## Scan History

| Date | Type | Findings | Health Score |
|------|------|----------|--------------|
| 2026-02-27 | security cycle 1 | 22 (2C/11H/8M/1L) | 42/100 |
| 2026-02-27 | security cycle 1 second pass | +7 (0C/1H/5M/1L) | 40/100 |
| 2026-02-27 | security cycle 1 third pass | +5 (0C/2H/2M/1L) | 55/100 |
| 2026-02-27 | quality cycle 2 | 20 (2C/8H/7M/3L) | 52/100 |
| 2026-02-27 | api cycle 3 | 11 (0C/3H/8M/0L) | 58/100 |
| 2026-02-27 | deps cycle 4 | 4 (0C/1H/1M/2L) | 58/100 |
| 2026-02-27 | quality cycle 6 | 16 (0C/5H/7M/4L) | 54/100 |
| 2026-02-27 | api cycle 7 | 10 (0C/1H/7M/2L) | 58/100 |
| 2026-02-28 | security cycle 8 | 10 (1C/4H/3M/2L) | 52/100 |
| 2026-02-28 | quality cycle 9 | 10 (0C/3H/5M/2L) | 53/100 |
| 2026-02-28 | api cycle 10 | 8 (0C/0H/5M/3L) | 55/100 |
| 2026-02-28 | deps cycle 11 | 9 (0C/3H/3M/3L) | 52/100 |
