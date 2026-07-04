# Security review — admin authorization & credential handling (2026-06-29)

Follow-up to the UX-polish/operator-control audit series (the integrations/api-key
findings flagged there for a dedicated review). This is a **verification-first**
review: every claim was confirmed against code (file:line) before action. The
accompanying PR **fixes the route-authorization gaps and the API-key issues**, and
documents the remaining items.

## Scope reframing (sets severity)

- **`/admin` is staff-only.** `require_admin_web_auth` (`app/web/auth/dependencies.py:196`)
  is default-deny to `system_user` principals (`STAFF_PRINCIPAL_TYPES`). Subscribers
  and resellers **cannot reach `/admin`**. The earlier audits' "any authenticated
  user" framing overstated it — the real exposure is **privilege escalation among
  staff**.
- **…but reduced-privilege staff roles really exist.** `scripts/seed/seed_rbac.py`
  seeds `admin` (wildcard `*`) plus `auditor`, `operator`, `support`,
  `finance_manager` with narrow permission sets; `require_permission` honors them
  (no implicit superuser for `system_user`). So a missing per-route guard let a
  `support`/`operator` staffer perform actions their role shouldn't — a **genuine**
  least-privilege bypass.
- **Why it drifted:** the build-failing arch test
  (`tests/architecture/test_route_permission_guards.py`) audits **only `/api/v1`**
  (line 98). The admin **web** surface was never covered, so per-route guard gaps
  there were invisible to CI even while adjacent routes in the same file guarded.

## Findings (all verified) and disposition

### 🔴 P0
| # | Finding | Evidence | Status |
|---|---------|----------|--------|
| 1 | **Secret-management routes unguarded** — read/save/create/delete OpenBao secrets | `system.py` secrets routes had no `require_permission` (siblings did) | **FIXED** — added admin-only `system:secrets:read`/`:write` (new perms, seeded; not granted to any non-admin role) |
| 2 | **Connector secrets plaintext at rest** | `app/models/connector.py:58` `auth_config` plain JSON; write path applies no `encrypt_credential` | **FIXED** — #534 `EncryptedJSON` encrypts on write / tolerant read; the 2 pre-migration plaintext prod rows re-encrypted + round-trip verified 2026-07-03 |
| 3 | **`/hooks/{id}/test` (+ hook create/edit/toggle) unguarded → `subprocess.run`** | route `integrations.py:1037`; exec `integration_hooks.py:443` | **FIXED** — guarded with `system:settings:write` (whole hooks group) |
| 4 | **Payment-provider create unguarded** | `integrations.py:1168` `POST /providers` | **FIXED** — `billing:provider:write` (perm already on `finance_manager`) |
| 5 | **API-key mint + revoke unguarded; revoke had no ownership check** | `system.py` api-keys routes | **FIXED** — `system:settings:write` + revoke now scoped to the caller's own keys |

### 🟠 P1
| # | Finding | Status |
|---|---------|--------|
| 6 | `/system/config/*` POST block unguarded (bank-transfer payee, RADIUS reject-rule push, billing/email/portal…) | **FIXED** — `system:settings:write` across the block |
| 7 | API-key one-time secret leaked via `?new_key=<raw>` URL (logs/history/Referer; re-shown on reload) | **FIXED** — secret now shown once in the POST response body; query param removed |
| 8 | Audit endpoint `x_api_key` branch skipped the audit-scope check JWTs must pass | **FIXED** — API keys no longer accepted by `require_audit_auth` (they carry no scope to satisfy it). **Behavior change** — see note |
| 9 | catalog_settings edit/delete/bulk-delete unguarded (create was guarded) | **FIXED** — `catalog:write` across all mutations |

### 🟡 P2
| # | Finding | Status |
|---|---------|--------|
| 10 | integrations connectors/register/installed/targets/jobs/webhooks/marketplace/whatsapp-test unguarded (siblings guarded) | **FIXED** — `system:settings:write` |
| 11 | API-key **hash mismatch** — admin-UI create bcrypt vs verify sha256 → UI keys never authenticated | **FIXED** — create now uses `hash_api_key` (sha256), matching verification |
| 12 | Connector `headers`/`metadata_` re-rendered via `\| tojson` (pasted tokens echoed) | **FIXED** — #540 `mask_secret_values`/`_unmask_secret_values` on the edit form (verified 2026-07-03) |
| 13 | API keys have **no scopes model** | **FIXED** — #539/#541 scopes column + wildcard-aware enforcement in `require_permission`; keys are first-class principals (verified 2026-07-03) |

### Refuted (correcting the earlier audit)
- **legal.py and gis.py are fully guarded** (every mutating route has
  `require_permission`; 12 and 19 refs). The system/config audit's "asymmetric
  guards" flag on legal/gis was a **false positive**.

## What this PR changes

- **61 admin-web routes guarded** across `system.py`, `integrations.py`,
  `catalog_settings.py` (secrets, api-keys, `/config/*`, company-info, providers,
  hooks, connectors/register/installed/targets/jobs/webhooks, catalog
  create/edit/delete/bulk-delete).
- **New admin-only permissions** `system:secrets:read` / `system:secrets:write`
  in `seed_rbac.py` (admin gets them via the all-perms grant; no non-admin role
  does).
- **API keys now work** (`hash_api_key` on create), the **raw secret never enters a
  URL** (rendered once on POST), and **revoke is owner-scoped**.
- **Audit endpoint no longer accepts API keys** (removed the unscoped branch).
- **Regression locks**: `tests/test_admin_route_permissions.py` extended with the
  sensitive admin-web routes; the audit-auth tests updated to assert API-key
  rejection. All targeted suites green; the `/api/v1` arch guard test still passes.

### ⚠️ Behavior change (finding #8)
API keys can no longer authenticate to the audit endpoint. They carry no scopes, so
they could never satisfy the audit-scope gate that bearer tokens must — accepting
them was the bypass. If a SIEM-style API-key integration needs audit access, it must
return via a **scoped API-key model** (finding #13), not the unscoped branch.

## Remaining work (recommended follow-ups)

1. **P0 — encrypt connector `auth_config` at rest** (#2) via `credential_crypto`
   (encrypt on write, decrypt at use), plus mask `headers`/`metadata_` on render
   (#12). Kept out of this PR because it touches the model + consumer paths and may
   need a one-off re-encrypt of existing rows; deserves its own reviewed change.
2. **Systemic — extend the build-failing arch test to `/admin` web routers.** This
   PR adds targeted regression locks for the sensitive routes; a blanket
   `/admin` guard test (with a pre-seeded burn-down quarantine, mirroring the
   `/api/v1` one) would make *all* future web-route gaps CI-visible.
3. **API-key scopes model** (#13) + per-key rate limit / max-keys / max-TTL / rotate
   flow (UX-polish backlog).
4. Move api-key hashing to HMAC-with-key for defense-in-depth (unsalted sha256 today;
   low crack risk given 256-bit tokens).
