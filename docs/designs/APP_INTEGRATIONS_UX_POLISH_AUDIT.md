# App integrations (connector lifecycle + API keys) — UX-polish & operator-control audit

> **Status: historical audit evidence.** Revalidate unresolved recommendations against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current domain SOT before implementation.

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review of the integration **connector
lifecycle** (marketplace → register → configure → install → targets/jobs/providers
+ registry) and the **API-keys / developer-access** surface.
**Status:** P0 + core P1 remediated (see "Remediation status" at the end). Completes the integrations coverage begun in
`INTEGRATIONS_WEBHOOKS_UX_POLISH_AUDIT.md` (which covered hooks/webhook-endpoints/
connector-config; CRM sync is in `CRM_IDENTITY_UX_POLISH_AUDIT.md`).

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. This domain is the **strongest case yet of "scaffolded but not wired"**
— a CRUD shell over a connector/target/job model whose execution layer is almost
entirely unimplemented (only `crm:pull_tickets` actually runs), plus two outright
correctness bugs and a cluster of security gaps that warrant a dedicated review.

## Acceptance criteria (app-integrations-specific)

1. Every connector/job/configure field either drives runtime behavior or is removed/
   relabeled as notes — no config the app never reads.
2. A key issued in the admin UI can actually authenticate; the one-time secret is
   shown once via a non-logged channel.
3. Secrets (connector auth, key material) are encrypted/hashed at rest; API keys
   carry enforceable scopes.
4. Health/observability reflect reality (no unconditional green, no dead "last used"/
   "latency" columns, names not raw UUIDs).
5. Every mutating route is permission-guarded.

## Cross-cutting themes

### POLISH

**P-A. Scaffolded features presented as working (the signature).**
- Generic integration jobs are non-functional: `run_sync_job` only handles
  `crm:pull_tickets`; other jobs no-op to **success** or raise "No sync adapter",
  yet the form collects `job_type/direction/trigger_mode/conflict_policy/entity_type/
  mapping_config/filter_config` — all ignored (`app/services/integration_sync.py:253-260`)
- register→configure saves `custom_fields/webhook_endpoint/auth_method/data_mapping`
  into `metadata.registration_config` — **no consumer anywhere**; operator
  "configures" an integration and nothing connects (`app/services/web_integrations.py:163-169`)
- "Relay Portal" per-connector toggle persists `relay_to_portal` — nothing consumes
  it (`templates/admin/integrations/installed.html:64-68`)
- Marketplace "Check for updates" is a no-op redirect; "Install" links to a **blank**
  `/connectors/new` (no pre-fill from the registry entry) (`app/web/admin/integrations.py:231-235`)

**P-B. Correctness bugs.**
- ⚠️ **Admin-issued API keys are dead on arrival**: the web flow stores a **bcrypt**
  hash (`hash_password`) but the only verification path matches a **sha256** hash
  (`hash_api_key`) — salted bcrypt can never match (`web_system_api_key_forms.py:32`
  vs `auth_dependencies.py:146`)
- A **disabled job still executes** on manual run: `run` logs `job_disabled` but does
  not `return`, proceeds to create an `IntegrationRun` and run it (`app/services/integration.py:272-273`)

**P-C. Fake / dead observability.**
- Installed "Health" is derived only from `WebhookDelivery` failure ratio → a
  connector with **no webhooks shows green/"Healthy"** unconditionally (`web_integrations.py:451-484`)
- Activity-log "Latency" reads `payload['latency_ms']` (never written → always "-");
  "Connector" column shows the raw **UUID** not the name (`web_integrations.py:546-552`)
- API-key `last_used_at` is never written on auth, but the list renders "Last used"
  (`auth_dependencies.py:152-156`, `templates/admin/system/api_keys.html:62`)

**P-D. Secret-handling UX & results.**
- API-key one-time secret passed as `RedirectResponse(...?new_key={raw})` → lands in
  browser history, access logs, Referer; re-renders on reload (`app/web/admin/system.py:2536`)
- Revoke ignores the boolean return, no flash; create renders `str(e)` (raw); copy
  button no toast/fallback
- List/create scoped to `person_id` only → a SystemUser-principal admin sees an empty
  list and create silently redirects (`system.py:2481,2523`)

### CONTROL

**C-1. Secrets at rest / missing model.**
- Connector `auth_config` (API keys, passwords, bearer tokens) is a plain `JSON`
  column, **unencrypted at rest** (`app/models/connector.py:58`) → encrypt
  (envelope / OpenBao ref like providers' `webhook_secret_ref`)
- API keys have **no scopes column**; auth returns `{actor_type,actor_id}` with no
  scopes → a key authenticates identity but carries **zero permissions** and can't be
  limited (`app/models/auth.py:244`, `auth_dependencies.py:156`)

**C-2. Config nothing consumes / hardcoded policy.**
- Job/register/relay fields above are dead inputs; registry catalog versions all
  hardcoded `"1.0.0"` (so "update available" can't fire); embed probe timeout `6.0s`
  hardcoded (`app/services/integrations/registry.py:23-54`)
- No API-key **rotate** flow (only create+revoke); no **max-keys-per-user**, no
  **max-TTL** (expiry choices hardcoded, "Never" allowed); per-key rate-limit / IP
  allowlist absent (`web_system_api_key_mutations.py`, `api_key_form.html:45-50`)
- Job `schedule_type`/`interval` only apply after a **Celery beat restart**, with no
  UI hint (`app/services/integration.py:393-398`)

### ⚠️ Security note (out of the two tracks — recommend a dedicated review)

The integrations router mounts with only `require_module_enabled("integrations")`;
many mutating routes lack `require_permission` while siblings have it:
`POST /connectors`, `/installed/bulk`, `/installed/{id}/relay`, `/installed/{id}/
uninstall`, `/register` + `/register/{id}/configure`, `/targets`, `/jobs`, and
notably **`POST /providers`** (creates a payment provider) (`app/web/admin/integrations.py`).
Likewise **`api_key_create` and `api_key_revoke`** are unguarded
(`app/web/admin/system.py:2512,2551`). Any authenticated admin with the module
enabled can create/uninstall connectors and create payment providers without
`system:settings:write`. Verify against the mount-registry RBAC layer; the asymmetry
itself is the finding. (Storage is hashed/masked correctly for keys — the bcrypt/
sha256 mismatch is the load-bearing bug, not plaintext.)

## Priority

| Tier | Items |
|------|-------|
| **P0** | Admin API keys DOA — bcrypt vs sha256 (P-B); API-key one-time secret via URL param (P-D, leak); connector `auth_config` plaintext at rest (C-1); `require_permission` missing on connector + api-key mutations incl. payment-provider create (Security) |
| **P1** | Stop presenting scaffolded features as working — gate/relabel generic jobs + register-configure + relay + marketplace to reality (P-A); API-key scopes model + enforcement (C-1); disabled job must not run on manual trigger (P-B); fix fake observability — health / last_used_at / latency / UUID→name (P-C); revoke/create results + friendly errors + system-user-owned keys (P-D) |
| **P2** | API-key rotate + max-keys + max-TTL (C-2); probe-timeout/catalog-version settings; schedule-restart hint; copy feedback; mask secret-looking header values |

## Appendix — full findings

### Connector lifecycle (marketplace/register/configure/install/targets/jobs/providers)
- [POLISH] (High) `app/services/integration_sync.py:253-260` + `jobs/new.html` — generic jobs non-functional (only crm:pull_tickets runs); other jobs no-op success or raise; form fields ignored → hide/disable unsupported types or relabel "CRM only" [recommend]
- [CONTROL] (High) `app/models/connector.py:58` — `auth_config` (keys/passwords/tokens) plain JSON, unencrypted at rest → encrypt (envelope/OpenBao ref) [recommend]
- [POLISH] (High) `app/services/web_integrations.py:163-169` + `register_configure.html` — registration_config saved but no consumer; auth_method free-text never mapped to connector.auth_type → wire to runtime or relabel "metadata only" [recommend]
- [POLISH] (Med) `installed.html:64-68` + `web_integrations.py:581-595` — "Relay Portal" toggle persists `relay_to_portal`, nothing consumes it → remove or implement [recommend]
- [POLISH] (Med) `web_integrations.py:451-484` — health from WebhookDelivery ratio only; connector w/o webhooks always green → compute from probe/last-run or "n/a" [recommend]
- [POLISH] (Med) `web_integrations.py:546-552` + `installed.html:126,130` — latency always "-" (never written); connector column shows raw UUID → drop/populate latency, resolve UUID→name [recommend]
- [POLISH] (Med) `app/web/admin/integrations.py:231-235` + `marketplace.html:17-19,63` — "Check for updates" no-op; "Install" links to blank `/connectors/new` → real re-scan + pre-seed form from registry [recommend]
- [CONTROL] (Med) `app/services/integration.py:272-273` — disabled job still runs on manual trigger (logs `job_disabled`, no return) → return/skip or refuse in UI [recommend]
- [CONTROL] (Med) `app/services/integration.py:393-398` — schedule changes need Celery beat restart, no UI hint → surface "requires restart / applied at" or hot-reload [defer]
- [CONTROL] (Low) `app/services/integrations/registry.py:23-54` + `web_integrations.py:716` — catalog versions hardcoded `1.0.0`; embed probe timeout `6.0s` hardcoded → probe timeout setting; real version source [defer]
- [POLISH] (Low) `integrations.py:402,459,729,1202` — create/configure errors render `str(exc)`; connector edit renders headers/metadata via `| tojson` (can expose pasted tokens) → friendly messages + mask secret-looking headers [defer]
- Security: integrations router only `require_module_enabled`; mutations missing `require_permission`: `/connectors`, `/installed/bulk|relay|uninstall`, `/register(+configure)`, `/targets`, `/jobs`, `/providers` (payment provider) → add `system:settings:write` / mount-level guard [recommend, security review]

### API keys / developer access
- [POLISH] (High) `web_system_api_key_forms.py:32` vs `auth_dependencies.py:146` — admin UI bcrypt-hashes key but verification matches sha256 → admin-issued keys can never authenticate → use `hash_api_key` in web create path [recommend]
- [POLISH] (High) `app/web/admin/system.py:2536` — one-time secret via `?new_key={raw}` → history/logs/Referer + re-renders on reload → single-shot flash/session value [recommend]
- [CONTROL/Security] (High) `system.py:2512,2551` — `api_key_create`/`api_key_revoke` (irreversible) have no `require_permission` (siblings do) → gate behind `system:settings:write` [recommend]
- [CONTROL] (High) `app/models/auth.py:244` + `auth_dependencies.py:156` — no scopes column; key auth returns no scopes → can't pass scoped `require_permission`, can't limit a key → model per-key scopes + inject into auth dict [recommend]
- [POLISH] (Med) `auth_dependencies.py:152-156` — `last_used_at` never written though list renders "Last used" → stamp on verify [recommend]
- [POLISH] (Med) `system.py:2551-2554` — revoke ignores boolean return, no flash → flash result [recommend]
- [POLISH] (Med) `system.py:2481,2523-2524` — list/create scoped to `person_id` only; SystemUser-principal admin sees empty list, create silent → support system-user-owned keys + message [recommend]
- [POLISH] (Med) `templates/admin/system/api_keys.html:32` — copy button no success toast / no fallback → confirmation + fallback [defer]
- [CONTROL] (Med) `web_system_api_key_mutations.py` — no rotate flow (only create+revoke) → add rotate (new secret, keep label/scopes) [defer]
- [CONTROL] (Med) `app/services/auth.py:55-56` — per-IP generate limit configurable (good) but no per-key rate limit, max-keys-per-user, or per-key IP allowlist → add max-keys-per-user (~10) + consider allowlist [defer]
- [CONTROL] (Low) `api_key_form.html:45-50` — expiry choices hardcoded (never/30/90/180/365), default "Never"; no org max-TTL → `api_key_max_ttl_days` setting, limit "Never" [defer]
- [POLISH] (Low) `system.py:2546` — create error renders `str(e)` → generic message + log [defer]
- Verified: key storage is hashed (not plaintext); read schema masks `key_hash`; list never renders the hash.


## Remediation status

### Resolved — security P0s (via the dedicated security PRs, 2026-06-29)

- Admin API keys DOA (bcrypt-on-create vs sha256-on-verify) + route guards on
  connector/API-key mutations incl. payment-provider create (#533)
- Connector `auth_config` encrypted at rest (#534)
- API-key scopes model + enforcement (#539); secret-looking header/metadata
  values masked on edit forms (#540); API keys as first-class principals on
  `require_permission` (#541)

### Resolved — UX/control pass (2026-07-03)

- **Disabled job ran anyway**: `integration_jobs.run` only logged (with a
  copy-pasted EMAIL_POLL message) and executed regardless; it now refuses with
  409, and both manual-run routes (jobs + syncs) check `is_active` first and
  show a "disabled — enable it before running" banner instead of "queued".
- **Relay Portal toggle removed** (persisted `relay_to_portal`, no consumer):
  column, route, and service writer deleted; template test updated.
- **Generic job form honesty**: free-text adapter/action inputs (which no-op'd
  or failed only at run time) replaced with a select of the actually-supported
  combos (`crm:pull_tickets`, or none/record-keeping); the create route
  validates and 400s anything else.
- **Register-configure relabeled "metadata only"** — the saved
  registration_config/auth_method has no runtime consumer; the form now says
  so instead of implying live configuration.

### Still open

- P-C fake observability (health / last_used_at / latency / UUID→name),
  P-D revoke/create result feedback + system-user-owned keys, and the P2 tail
  (rotate, max-keys/TTL, probe-timeout settings, copy feedback).
