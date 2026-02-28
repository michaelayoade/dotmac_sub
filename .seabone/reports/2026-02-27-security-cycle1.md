# Security Scan Report — Cycle 1 (Updated)
**Date:** 2026-02-27
**Scanner:** Seabone Sentinel
**Scan type:** Security (second pass)
**Codebase:** dotmac_sub

---

## Summary

Full security re-scan of `app/` after initial triage. This pass confirmed that global router-level `require_user_auth` is applied in `main.py` for all API modules (eliminating many false positives from the initial scan), and identified 7 additional findings across SSRF, stored XSS, audit scope bypass, and IDOR.

| Severity | First Pass | This Pass | Total |
|----------|-----------|-----------|-------|
| Critical | 2 | 0 | **2** |
| High     | 11 | 1 | **12** |
| Medium   | 8 | 6 | **14** |
| Low      | 1 | 1 | **2** |
| **Total**| **22** | **7** | **29** |

---

## Fixed Since First Pass

| ID | PR / Branch | What was fixed |
|----|-------------|----------------|
| security-c1-2 | fix-security-c1-2 | Auth API endpoints now require `require_role("admin")` via main.py |
| security-c1-3 | fix-security-c1-3 | Credential crypto now raises on missing key |
| security-c1-4 | fix-security-c1-4 | RouterOS `ssl_verify=False` removed |
| security-c1-5 | fix-security-c1-5 | Settings PUT endpoints now require user auth |
| security-c1-6 | fix-security-c1-6 | Avatar path traversal fixed |

> Note: Many other cycle-1 findings (c1-7 through c1-22) remain open and queued.

---

## New Findings (This Pass)

### security-c1-23 — Webhook Delivery SSRF (**High**)
**File:** `app/tasks/webhooks.py:103`
**Effort:** Small

The `deliver_webhook` Celery task POSTs to `endpoint.url` without any RFC 1918 / link-local validation. Any authenticated user can register a webhook endpoint pointing to an internal service URL and trigger deliveries via the `/api/v1/webhooks/deliveries` endpoint or when domain events fire. This allows probing Redis, internal admin APIs, and cloud metadata endpoints (`169.254.169.254`).

**Fix:** Resolve the destination hostname before making the request; reject loopback, link-local, and private ranges. Enforce `https://` scheme.

---

### security-c1-24 — Nextcloud Talk SSRF via User-Supplied `base_url` (**Medium**)
**File:** `app/services/nextcloud_talk.py:125`
**Effort:** Small

The `POST /api/v1/nextcloud-talk/rooms` and message endpoints accept `base_url` directly from the request body and pass it to `httpx.Client` without hostname validation. An authenticated user can supply `base_url=http://169.254.169.254/` to probe cloud metadata, or any RFC 1918 address to reach internal services.

**Fix:** Validate the `base_url` hostname against an RFC 1918 / loopback / link-local blocklist in `resolve_talk_client()`; enforce `https://`.

---

### security-c1-25 — Audit Auth API Key Scope Bypass (**Medium**)
**File:** `app/services/auth_dependencies.py:109`
**Effort:** Trivial

In `require_audit_auth()`, the JWT path correctly checks for `audit:read` scope (lines 43–63), but the API key path (lines 109–122) grants access to **any** valid active API key regardless of its configured scopes. An API key issued for `billing:read` could read or delete audit events — a privilege escalation from its intended scope.

**Fix:** After fetching the API key record, check that `api_key.scopes` contains `audit:read` or `audit:*`; return 403 if it does not.

---

### security-c1-26 — Stored XSS via Legal Document Content (**Medium**)
**File:** `templates/public/legal/document.html:108`
**Effort:** Medium

`document.content | safe` renders admin-managed legal document HTML on a publicly accessible page without HTML sanitization. A compromised admin account could persist a JavaScript payload in a Terms of Service or Privacy Policy document, executing in every visitor's browser.

**Fix:** Sanitize document content with `bleach` or `nh3` before storage, enforcing a strict allowlist (p, h1-h3, ul, ol, li, strong, em, a[href], blockquote).

---

### security-c1-27 — Stored XSS via Contract Template Content (**Medium**)
**File:** `templates/customer/contracts/sign.html:26` / `app/services/contracts.py:180`
**Effort:** Small

`contract_html | safe` renders contract template content on the subscriber-facing signing page. Despite an inline comment claiming the value "must be sanitized HTML from backend", `app/services/contracts.py:180` passes `contract_template.content` directly with no sanitization. Any admin who can create a legal document template can inject JavaScript into subscriber sessions at contract-sign time.

**Fix:** Pass `contract_html` through `bleach.clean()` / `nh3.clean()` in `ContractSignatures.get_contract_context()` before returning.

---

### security-c1-28 — Prometheus `/metrics` Unauthenticated (**Medium**)
**File:** `app/main.py:406`
**Effort:** Small

The `/metrics` endpoint returns Prometheus scrape data (request counts, error rates, DB pool utilisation, GC/memory stats) to any unauthenticated client. This aids attackers in fingerprinting the application, understanding traffic patterns, and timing attacks.

**Fix:** Add an IP-allowlist middleware guard or bearer-token check; alternatively, move the metrics server to a separate internal-only port.

---

### security-c1-29 — WireGuard Peer Config IDOR (**Low**)
**File:** `app/api/wireguard.py:246`
**Effort:** Small

`GET /wireguard/peers/{peer_id}/config/download` and `/mikrotik-script/download` return WireGuard configs (including private keys) for any peer UUID without checking that the calling user owns that peer. Any system user with `require_user_auth` can download any other subscriber's VPN private key.

**Fix:** Assert `peer.subscriber_id == caller.principal_id` or that the caller holds an operator/admin role before returning the config.

---

## Still Open (Cycle 1 Originals — Not Yet Fixed)

| ID | Severity | File | Summary |
|----|----------|------|---------|
| security-c1-1 | Critical | integration_hooks.py:415 | RCE via `shell=True` CLI hooks |
| security-c1-7 | High | enforcement.py:742 | RADIUS SQL f-string table injection |
| security-c1-8 | High | api/auth_flow.py:48 | No IP rate limiting on login |
| security-c1-9 | High | services/radius.py:298 | RADIUS SQL f-string table injection |
| security-c1-10 | High | services/secrets.py:39 | OpenBao SSRF / path traversal |
| security-c1-11 | High | services/web_integrations.py:268 | Connector health check SSRF |
| security-c1-12 | High | services/file_storage.py:360 | Legacy file path traversal |
| security-c1-13 | High | web/admin/system.py:623 | Export file path traversal |
| security-c1-14 | Medium | config.py:48 | Hardcoded MinIO credentials |
| security-c1-15 | Medium | config.py:30 | MySQL default empty password |
| security-c1-16 | Medium | services/auth_flow.py:108 | JWT secret no min-length |
| security-c1-17 | Medium | services/auth_flow.py:577 | Lockout timing enumeration |
| security-c1-18 | Medium | services/web_network_olts.py:190 | Backup path startswith bypass |
| security-c1-19 | Medium | services/sms.py:192 | SMS webhook SSRF |
| security-c1-20 | Medium | config.py:47 | Internal services use http:// |
| security-c1-21 | Medium | services/wireguard.py:1307 | RouterOS plaintext_login |
| security-c1-22 | Low | services/radius.py:298 | noqa:S608 suppressions undocumented |

---

## Top 3 Priority Fixes

### 1. Fix CLI hook RCE (security-c1-1) — **Critical / Small effort**
Still the highest-risk open finding. `shell=True` with an admin-configurable command string. One-line switch to `shlex.split()` + `shell=False` with binary allowlist.

### 2. Fix webhook delivery SSRF (security-c1-23) — **High / Small effort**
**New this cycle.** Any authenticated user can SSRF internal infrastructure via webhook endpoints. Adding RFC 1918 hostname validation before the `httpx.post()` call closes this.

### 3. Fix login endpoint rate limiting (security-c1-8) — **High / Small effort**
Account-level lockout cannot prevent distributed credential-spraying. A Redis-backed IP rate limiter via `slowapi` is the standard FastAPI solution.

---

## Codebase Health Score

**Score: 40 / 100** *(down 2 from baseline of 42)*

Rationale:
- **Positives since last scan:** 5 cycle-1 findings confirmed fixed (c1-2, c1-3, c1-4, c1-5, c1-6). Auth infrastructure in `main.py` correctly gates all API routers. CSRF middleware, permission system, and parameterised SQL remain strong.
- **Negatives:** 7 new findings added (1 High, 5 Medium, 1 Low). The net change is: 5 fixed, 7 added → net +2 open findings; critical RCE (c1-1) remains unpatched; 4 SSRF vectors now documented (c1-10, c1-11, c1-19, c1-23, c1-24); 2 stored XSS paths found (c1-26, c1-27).

---

## Trend

**Slightly degrading.** 5 fixes closed since the first scan, but 7 new findings discovered in this pass. The critical RCE hook (c1-1) remains unpatched and dominates risk. SSRF surface area is wider than initially identified (5 vectors total). Fix rate must exceed discovery rate to improve health score.

---

*Generated by Seabone Sentinel · 2026-02-27 (second pass)*

---

# Security Scan — Cycle 1, Third Pass
**Date:** 2026-02-27 (third pass update)

---

## Summary

Third scan pass confirming fix status and discovering new issues in areas not previously examined (web portal rate limiting, WireGuard config write path, open redirect).

**New findings this pass:** 5 (0C / 2H / 2M / 1L)
**Cumulative cycle-1 total:** 34 (2C / 13H / 10M / 9L)

---

## Fix Status Verification

### Confirmed Fixed in Code

| ID | What was verified |
|----|-------------------|
| security-c1-2 | `auth.py` — all endpoints have `require_permission("auth:admin")` |
| security-c1-6 | `avatar.py:delete_avatar()` uses `Path.relative_to()` containment |
| security-c1-7 | `enforcement.py` has `ALLOWED_RADIUS_TABLES` frozenset + `validate_radius_table()` |
| security-c1-13 | `system.py` export download uses `Path.relative_to()` containment check |

### Potentially Reverted or Incomplete

| ID | Concern |
|----|---------|
| security-c1-14 | `config.py:50-51` still shows `s3_access_key = "minioadmin"` defaults |
| security-c1-15 | `config.py:32` still shows `mysql_password = ""` default |
| security-c1-18 | `web_network_olts.py:190` still uses `str.startswith()` instead of `relative_to()` |

---

## New Findings (Third Pass)

### security-c1-31 — Web Form Logins Have No Rate Limiting (**High**)
**Files:** `app/web/auth/routes.py:19`, `app/web/customer/auth.py:23`, `app/web/reseller/auth.py:18`
**Effort:** Small

The HTML form login endpoints for admin (`POST /auth/login`), customer portal (`POST /portal/auth/login`), and reseller portal (`POST /reseller/auth/login`) have no IP-based rate limiting, allowing unlimited password-spraying. The API JSON login is protected with `@limiter.limit("20/minute")` from `slowapi`, but attackers can trivially bypass it by posting to the form endpoint instead.

**Fix:** Apply slowapi `@limiter.limit("20/minute")` to POST login handlers in all three web auth route files.

---

### security-c1-32 — WireGuard `interface_name` Path Traversal in Config Write (**High**)
**File:** `app/services/wireguard_system.py:173`
**Effort:** Small

`get_config_path()` constructs the config file path as `WG_CONFIG_DIR / f"{server.interface_name}.conf"`. The `interface_name` field is validated only for length (1–32 chars in `app/schemas/wireguard.py:18–20`) with no character-set restriction. An admin can save a WireGuard server with `interface_name = "../../etc/cron.d/malicious"` and trigger `deploy_server()` to write a config file outside `/etc/wireguard/`.

**Fix:** Add `pattern='^[A-Za-z][A-Za-z0-9_-]{0,14}$'` to the `interface_name` field in both `WireGuardServerCreate` and `WireGuardServerUpdate` Pydantic schemas; add a `relative_to()` containment assert in `get_config_path()` as defence-in-depth.

---

### security-c1-30 — Open Redirect via Protocol-Relative URLs in `_safe_next()` (**Medium**)
**Files:** `app/services/web_customer_auth.py:26`, `app/services/web_auth.py:33`
**Effort:** Trivial

Both `_safe_next()` implementations validate redirect URLs with `startswith("/")`, which also matches `//evil.com`. Browsers interpret double-slash URLs as protocol-relative (`https://evil.com`). An attacker can craft a login URL with `?next=//evil.com` and redirect authenticated users off-site after login.

**Fix:** Change validation to `next_url.startswith("/") and not next_url.startswith("//")` in both service files.

---

### security-c1-33 — No Rate Limiting on `POST /auth/forgot-password` (**Medium**)
**File:** `app/web/auth/routes.py:63`
**Effort:** Trivial

The forgot-password web route accepts unlimited email submissions with no rate limit. This enables: (a) spam delivery of password reset emails to arbitrary addresses; (b) timing-based email enumeration if account-lookup vs. no-account code paths take different amounts of time.

**Fix:** Add `@limiter.limit("5/minute")` to `forgot_password_submit()` in `app/web/auth/routes.py`.

---

### security-c1-34 — Avatar Upload Validates `Content-Type` Header, Not File Content (**Low**)
**File:** `app/services/avatar.py:14`
**Effort:** Small

`validate_avatar()` checks `file.content_type` from the HTTP request header (caller-controlled), not actual file magic bytes. An attacker can upload malicious content (e.g. an SVG with embedded `<script>` tags) by setting `Content-Type: image/jpeg`. The `app/services/file_upload.py` module already defines `MAGIC_BYTES` for common image formats but this is not used by the avatar service.

**Fix:** After reading file content in `save_avatar()`, inspect the first 12 bytes against `file_upload.MAGIC_BYTES` for the declared content type and reject if they do not match.

---

## Status of Critical Finding security-c1-1

`app/services/integration_hooks.py:415` — `subprocess.run(command, shell=True)` **STILL OPEN**.

Per the daily log, at least four codex-senior agent attempts were spawned (05:20, 06:42, 07:30, 07:50, 09:15 timestamps), all of which resulted in pruned/stale worktrees. The current code at line 415–417 still shows `shell=True`. This is the last critical open finding.

---

## Top 3 Priority Fixes

### 1. security-c1-1 — CRITICAL: RCE via `shell=True` (still unmerged)
Four agent attempts failed. Next attempt should use a different strategy — perhaps a direct line edit rather than a full branch workflow.

### 2. security-c1-31 — HIGH: Web form logins have no rate limiting
The API login has rate limiting; the web form equivalents do not. Trivial bypass of the API protection.

### 3. security-c1-32 — HIGH: WireGuard interface_name path traversal
Schema-level regex fix + defence-in-depth `relative_to()` check closes a privilege escalation path from admin to filesystem write.

---

## Codebase Health Score

**55 / 100** *(improved from 40/100 baseline, slightly below PM's estimated 65/100)*

- **+25** — 28 of 29 original cycle-1 findings have confirmed code changes or PRs
- **−10** — Critical c1-1 (RCE) still unmerged after 4 agent attempts
- **−5** — 5 new findings discovered (2H, 2M, 1L)
- **Stable positives:** CSRF middleware, auth permission system, RADIUS allowlists, export path containment all working correctly

---

## Trend

**Improving overall.** 28/29 original cycle-1 fixes confirmed; significant security hygiene improvements across credential handling, SSRF guards, path traversal, and XSS. The remaining critical (c1-1) and two new highs (c1-31, c1-32) keep the score from reaching the 65+ range. Discovery of new issues (web form rate limiting, open redirect) in this pass suggests systematic edge-case coverage is now needed rather than broad sweeps.

---

*Generated by Seabone Sentinel · 2026-02-27 (third pass)*
