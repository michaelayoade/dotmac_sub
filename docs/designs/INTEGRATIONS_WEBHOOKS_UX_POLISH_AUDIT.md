# Integrations & webhooks — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of integrations (connectors/hooks/webhook
endpoints) + the system webhooks UI. (CRM sync/dead-letters and the WhatsApp *send*
path are covered in the CRM and Notifications audits.)
**Status:** remediation in progress via draft PR #520. Part of the remaining-module audit series.

## Remediation status

**Last updated:** 2026-06-29
**Tracking PR:** #520 (`audit/integrations-webhooks-remediation`)

### Resolved in current draft

- System webhook create/update now persists selected `WebhookEventType`
  subscriptions instead of rendering a dead checkbox grid.
- System webhook edit no longer renders the stored encrypted signing secret back
  into the password field; leaving the field blank preserves the current secret.
- System webhook event choices now render from the enum-backed event list instead
  of a hardcoded static subset.
- Integrations webhook endpoint creation now encrypts signing secrets at rest,
  the new form uses a password field, failed submissions do not echo the secret,
  and detail pages show only configured/not-configured state.
- Connector detail pages now expose the existing Check Connection probe action
  directly instead of hiding it behind the embedded view.
- Integrations webhook endpoints now have edit, enable/disable, soft-delete,
  rotate-secret, and test-delivery actions from the list/detail surfaces.
- Hook auth secrets now use credential-at-rest wrapping for bearer/basic/HMAC
  values, execute with decrypted values, and no longer render stored secrets
  back into the hook edit form.
- Integrations webhook detail pages now summarize latest delivery, latest
  failure, and recent delivered/pending/failed outcomes above the delivery log.
- Installed integration bulk enable/disable, relay toggle, integration
  enable/disable, and hook enable/disable actions now prompt before changing
  state.
- Integration hooks now have a bounded per-hook execution timeout control that
  drives both HTTP and CLI hook execution while preserving legacy defaults for
  existing rows.

### Still open

- Webhook delivery retry/timeout settings and the separate RBAC review remain
  open.

### Verification

- `poetry run ruff check app/services/web_system_webhook_forms.py app/web/admin/system.py tests/test_web_system_webhook_forms.py`
  - Result: passed
- `poetry run pytest tests/test_web_system_webhook_forms.py -q`
  - Result: `3 passed`
- `poetry run pytest tests/test_webhook_services.py tests/test_core_services_extra.py -q`
  - Result: `9 passed`
- `poetry run pytest tests/test_admin_route_permissions.py -q`
  - Result: `15 passed`
- `poetry run ruff check app/services/web_integrations.py tests/test_web_integrations_webhooks.py`
  - Result: passed
- `poetry run pytest tests/test_web_integrations_webhooks.py -q`
  - Result: `7 passed`
- `poetry run pytest tests/test_webhook_services.py tests/test_core_services_extra.py tests/test_web_system_webhook_forms.py -q`
  - Result: `12 passed`
- `poetry run ruff check tests/test_integration_hooks_web_admin.py tests/test_web_integrations_webhooks.py`
  - Result: passed
- `poetry run pytest tests/test_integration_hooks_web_admin.py tests/test_web_integrations_webhooks.py -q`
  - Result: `14 passed`
- `poetry run ruff check app/models/integration_hook.py app/services/integration_hooks.py app/web/admin/integrations.py tests/test_integration_hooks_service.py tests/test_integration_hooks_web_admin.py alembic/versions/186_integration_hook_timeout_seconds.py`
  - Result: passed
- `poetry run pytest tests/test_integration_hooks_service.py tests/test_integration_hooks_web_admin.py -q`
  - Result: `13 passed`
- `poetry run pytest tests/test_admin_route_permissions.py -q`
  - Result: `15 passed`
- `poetry run ruff check app/services/integration_hooks.py app/web/admin/integrations.py tests/test_integration_hooks_service.py tests/test_integration_hooks_web_admin.py`
  - Result: passed
- `poetry run pytest tests/test_integration_hooks_service.py tests/test_integration_hooks_web_admin.py -q`
  - Result: `11 passed`
- `poetry run pytest tests/test_credential_key_rotation_service.py -q`
  - Result: `17 passed`
- `poetry run pytest tests/test_admin_route_permissions.py -q`
  - Result: `15 passed`
- `poetry run ruff check app/services/web_integrations.py tests/test_web_integrations_webhooks.py`
  - Result: passed
- `poetry run pytest tests/test_web_integrations_webhooks.py -q`
  - Result: `2 passed`
- `poetry run pytest tests/test_webhook_services.py tests/test_core_services_extra.py tests/test_web_system_webhook_forms.py -q`
  - Result: `12 passed`
- `poetry run pytest tests/test_web_integrations_webhooks.py -q`
  - Result: `3 passed`
- `poetry run ruff check app/services/web_integrations.py app/web/admin/integrations.py tests/test_web_integrations_webhooks.py`
  - Result: passed
- `poetry run pytest tests/test_web_integrations_webhooks.py -q`
  - Result: `6 passed`
- `poetry run pytest tests/test_webhook_services.py tests/test_core_services_extra.py tests/test_web_system_webhook_forms.py -q`
  - Result: `12 passed`
- `poetry run pytest tests/test_admin_route_permissions.py -q`
  - Result: `15 passed`

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. Headline: **two parallel, divergent webhook UIs** —
`/admin/system/webhooks` (encrypts secrets but its event-subscription UI doesn't
work) and `/admin/integrations/webhooks` (plaintext secrets, create-only) — plus a
secret-corruption-on-edit bug.

## Acceptance criteria (integrations-specific)

1. One webhook UI/contract; secrets encrypted at rest everywhere and never
   re-rendered into the form.
2. Every config control changes behavior — the event-subscription grid actually
   creates subscriptions.
3. Endpoints/connectors are remediable: edit, disable, rotate-secret, delete, and
   test from the UI.
4. Delivery retry/timeout are configurable; delivery has observability
   (last-delivery, failures).

## Cross-cutting themes

### POLISH

**P-A. Dead / corrupting controls.**
- `/admin/system/webhooks` "Event Subscriptions" checkbox grid is a **dead control**
  — `webhook_create/update` never read `events`, no `WebhookSubscription` rows are
  created → the endpoint can never fire (`templates/admin/system/webhook_form.html:78-143`, `app/web/admin/system.py:2603-2635`)
- On edit, the password field is pre-filled with the stored **encrypted** secret;
  saving without retyping re-encrypts the ciphertext → corrupts the signing secret →
  wrong delivery HMAC (`webhook_form.html:53`, `web_system_webhook_forms.py:85-87`)

**P-B. No remediation / observability on the integrations webhook surface.**
- Integrations webhook endpoints are **create-only**: View only; no edit/disable/
  rotate/delete/test (`templates/admin/integrations/webhooks/{index,detail}.html`)
- No "send test event" for webhook endpoints (only hooks have `/test`); no
  last-delivery/failure summary (`app/web/admin/integrations.py:1116-1137`)
- Connector detail has no "Check Connection" (the probe is only wired into the embed
  page) (`templates/admin/integrations/connectors/detail.html`)

### CONTROL

**C-1. Secret storage inconsistent / plaintext at rest (security).**
- Integrations path persists the webhook secret **plaintext**
  (`WebhookEndpointCreate(secret=secret.strip())`, no `encrypt_credential`) while the
  system path encrypts into the same column; `new.html:68` even uses `type="text"`
  (`app/services/web_integrations.py:950-958`)
- Hook auth secrets (bearer/basic/HMAC) stored plaintext in `auth_config` JSON and
  rendered back plaintext (`app/services/integration_hooks.py:150,192`)

**C-2. Delivery retry/timeout hardcoded.** Webhook delivery `MAX_RETRIES=10`,
`RETRY_DELAYS`, HTTP `30.0` hardcoded with no settings/per-endpoint override (hooks
expose per-hook `retry_max`/`retry_backoff_ms`) (`app/tasks/webhooks.py:23-25,96`);
hook exec timeouts `10.0`/`20` hardcoded (`integration_hooks.py:429,447`).

### Security note (out of the two tracks)

Many integrations write routes lack `require_permission` guards their siblings have:
`connector_create`, `target_create`, `job_create`, all `/hooks` create/update/
**test/toggle** (note `/hooks/{id}/test` runs `subprocess.run` for CLI hooks —
`integration_hooks.py:443`), `webhook_create`, `installed bulk/uninstall/relay`,
`whatsapp test-send`. Verify against the mount-registry RBAC layer; if not covered
this is an **RBAC/RCE-adjacent gap** warranting a dedicated security review.

## Priority

| Tier | Items |
|------|-------|
| **P0** | system/webhooks event-subscription grid is dead → endpoints never fire (P-A); encrypted-secret prefill corrupts signing secret on save (P-A); integrations webhook secret plaintext at rest (C-1) |
| **P1** | Unify the two webhook UIs (encrypt+mask everywhere, edit/disable/rotate/delete/test, subscriptions from `WebhookEventType` enum); encrypt hook auth secrets (C-1); test-event + delivery observability + connector check-connection (P-B); retry/timeout → settings (C-2) |
| **P2** | secret-mask consistency, confirms on disable/bulk/relay/rotate, hook exec timeouts; (separate) RBAC review of unguarded integrations routes |

## Appendix — full findings
- [POLISH] (High) `templates/admin/system/webhook_form.html:78-143` + `system.py:2603-2635` + `web_system_webhook_forms.py:47-66` — event-subscription grid never read; no `WebhookSubscription` rows → endpoint never fires → read `events`, create/sync subscriptions [recommend]
- [POLISH] (High) `webhook_form.html:53` + `web_system_webhook_forms.py:85-87` — edit pre-fills encrypted secret; save without retype re-encrypts ciphertext → corrupts signing secret → never render secret, "leave blank to keep" [recommend]
- [POLISH] (High) `templates/admin/integrations/webhooks/{index,detail}.html` — create-only; no edit/disable/rotate/delete/test → add remediation actions [recommend]
- [CONTROL] (High) `app/services/web_integrations.py:950-958` — integrations webhook secret stored plaintext (system path encrypts same column); `new.html:68` `type="text"` → standardize on `encrypt_credential`, mask [recommend]
- [CONTROL] (Med) `app/tasks/webhooks.py:23-25,96` — delivery `MAX_RETRIES=10`/`RETRY_DELAYS`/`30s` hardcoded, no per-endpoint override (hooks have it) → timeout setting + per-endpoint retry [recommend]
- [CONTROL] (Med) `app/services/integration_hooks.py:150,192` + `hooks/form.html:99,111` — hook auth secrets plaintext in `auth_config`, rendered plaintext → encrypt + mask (leave-blank-to-keep) [recommend]
- [CONTROL] (Med) `integration_hooks.py:429,447` — hook exec timeouts `10s`/`20s` hardcoded though retries configurable → per-hook timeout field (bounded) [defer]
- [POLISH] (Med) `app/web/admin/integrations.py:1116-1137` + `web_integrations.py:1124` — no test-event for webhook endpoints; no last-delivery/failure summary → test-delivery button + badges [recommend]
- [POLISH] (Med) `templates/admin/integrations/connectors/detail.html` — no Check-Connection on plain connector detail (probe only on embed) → surface Check-Connection action [recommend]
- [POLISH] (Low) `templates/admin/integrations/webhooks/detail.html:41` — mask `'****'+secret[-4:]` shows ciphertext tail (encrypted) / plaintext tail (integrations) → mask uniformly (set/not-set) [defer]
- [POLISH] (Low) `templates/admin/system/webhooks.html:106-139` + form — event list a hardcoded static set that drifts from `WebhookEventType` enum → render both from enum [defer]
- [POLISH] (Low) `app/web/admin/integrations.py:285-301` + `installed.html` — no confirm before disable/bulk-disable/relay/rotate (only uninstall confirms) → add confirms [defer]
- Verified: hooks surface is most complete (test, retry config, exec log).
