# CRM sync & customer identity — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) CRM sync/tickets/webhook/
dead-letter/duplicate-merge + the Syncs admin UI, (b) customer-identity
normalization/resolution + CRM customer upsert / portal / billing-push.
**Status:** audit only. Part of the remaining-module audit series.

> Known: full bidirectional CRM sync is deployed (incremental pull w/ watermark,
> ticket/comment push, webhooks, billing snapshots); the `crm.ticket_pull`
> feature-flag resolution bug was recently fixed (PR #506).

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. This domain is the strongest example yet of the **dead/misleading
control** signature — multiple sync-config selectors are persisted but never read.

## Acceptance criteria (CRM/identity-specific)

1. Exactly one scheduler drives each CRM pull; UI schedule and beat config can't
   double-run or collide on the run-guard.
2. Every sync-config selector shown either changes behavior or is removed/labeled
   as fixed (no dead controls).
3. Terminal sync failures (dead-letters) are visible and re-drivable from the UI.
4. Webhook-driven overwrites of customer identity are audited (old→new per field).
5. Identity/currency/country policy is read from registered settings, not env
   constants.

## Cross-cutting themes

### POLISH

**P-A. Built-but-unsurfaced + dead controls** (the signature).
- Dead-letter visibility is fully wired in the route (`crm_dead_letter_count`,
  25-row list, `/crm-dead-letters/redrive` POST) but **no template renders it** and
  there's no re-drive button — operators are blind to terminal push failures
  (`app/web/admin/integrations.py:61-62`)
- "Conflict Policy" selector (remote/local/newest/manual) persisted but **never
  read**; behavior is hardcoded remote-wins (`app/services/web_integration_syncs.py:245`, `templates/admin/integrations/syncs/detail.html:111-118`)
- "Ambiguous Match Policy" offers skip/manual_review but logic always skips;
  `mapping_primary`/`mapping_fallback` are free-text the resolver ignores (`syncs/detail.html:140-146`, `crm_ticket_pull.py:446`)

**P-B. Sync run observability.**
- Run metrics show only fetched/created/updated/skipped; stored
  `errors`/`comments_created`/`skipped_leads`/`mode`/`since` watermark are not
  shown, and a run with per-ticket errors but `fetched>0` is marked **success**
  (`syncs/detail.html:216-221`, `integration_sync.py:228-239`)
- Re-drive route ignores `redrive()`'s boolean return and always redirects with no
  success/failure flash (`app/web/admin/integrations.py:74-84`)
- CRM customer match decision (matched-via / created-new) is not logged — no
  observability into update-vs-duplicate (`crm_customers.py:145-174`)
- `reseller_open_tickets_count` returns `0` on any CRM error — indistinguishable
  from a genuine 0 → silent under-report during outages (`crm_portal.py:508-533`)

**P-C. No audit on identity overwrite (data hazard).** Every CRM webhook silently
overwrites name/email/phone/address/status with no audit row or change log — the
documented "merge two customers" hazard, with no way to see or revert
(`crm_customers.py:177-206`).

**P-D. Confirms on expensive actions.** "Run Now" (manual full sweep) and
"re-drive all" trigger immediately, no confirm (`syncs/index.html:87`). (Duplicate-
merge is CLI-only with dry-run default — no gap.)

### CONTROL

**C-1. Two schedulers can drive the same pull (config correctness).** A dedicated
beat entry runs the **incremental** pull (watermark, limit=200/max_pages=50); if an
operator sets the sync-profile job to `interval` in the UI, `list_interval_jobs`
registers a **second** beat entry running a **non-incremental full** pull. Both
write `IntegrationRun` rows and collide on the "already running" guard
(`app/services/scheduler_config.py:1173` vs `:1872`).

**C-2. Configurable-looking knobs the real run ignores.**
- The scheduled (incremental) pull uses task defaults and **never reads the job's
  `filter_config`**, so the detail-page Batch-Size / Max-Pages / Pull-Comments only
  affect manual "Run Now" (`app/tasks/crm_ticket_pull.py:12-15`, `integration_sync.py:122-124`)

**C-3. Env-only constants bypassing settings.**
- CRM cache TTL / retry policy / reachability-circuit cooldown read via `os.getenv`
  at import, not registered settings (`crm_client.py:26-36,80`)
- `crm_billing_push` currency via `os.getenv("BILLING_DEFAULT_CURRENCY")` bypassing
  the registered `billing.default_currency` (`crm_billing_push.py:54`)
- `DEFAULT_COUNTRY_CODE="234"` hardcoded for all phone canonicalization though
  `billing.default_country_code` exists (`customer_identity_normalization.py:19,30`)

**C-4. Hardcoded identity/matching policy.**
- Sensitive-automation gate hardcodes `{HIGH, MEDIUM}` acceptance — no HIGH-only
  option (`customer_identity_resolution.py:84-85,159`)
- CRM fuzzy-match uses phone exact-string equality (no normalization, unlike the
  resolution module) + mandatory name equality (`crm_customers.py:158-172`)
- `crm_billing_push` has no enable/direction control (always runs) (`crm_billing_push.py:60`)

### CONTROL/POLISH — duplicate auth path
- `crm_webhook` keeps its own JWT cache + hardcoded 10s/15s timeouts, no
  retry/circuit, unlike `CRMClient` → route pushes through `CRMClient` (`crm_webhook.py:32,76`)

## Priority

| Tier | Items |
|------|-------|
| **P0** | Two-scheduler collision → double/duplicate pulls + run-guard collision (C-1); webhook silent identity overwrite, no audit (P-C, merge/data hazard); dead-letter panel not rendered — terminal failures invisible (P-A) |
| **P1** | Fix-or-remove dead controls: conflict-policy, ambiguous-match, mapping fields, scheduled-pull `filter_config` (P-A/C-2); sync run observability incl. success-masks-errors + redrive flash + match logging (P-B); `DEFAULT_COUNTRY_CODE` + `crm_billing_push` currency → settings (C-3); sensitive-automation confidence as a setting (C-4) |
| **P2** | CRM cache/retry/circuit env→registered settings (C-3); fuzzy-match phone normalize (C-4); `crm_billing_push` enable flag; Run-Now/redrive-all confirms (P-D); outage-returns-0 → "unavailable" (P-B); route webhook via CRMClient |

## Appendix — full findings

### CRM sync / tickets / webhook / dead-letter
- [POLISH] (High) `app/web/admin/integrations.py:61-62` + connectors overview — dead-letter count/list + redrive POST wired but no template renders them, no button → render dead-letter panel + per-row & re-drive-all [recommend]
- [CONTROL] (High) `web_integration_syncs.py:245` + `syncs/detail.html:111-118` — Conflict Policy selector persisted, never read; hardcoded remote-wins → implement in `sync_ticket` or remove + label fixed [recommend]
- [CONTROL] (High) `scheduler_config.py:1173` vs `:1872` — UI `interval` job registers a second (full, non-incremental) pull alongside the dedicated incremental beat; collide on run-guard → single source / hide schedule for CRM job [recommend]
- [CONTROL] (Med) `app/tasks/crm_ticket_pull.py:12-15` + `integration_sync.py:122-124` — scheduled incremental pull ignores job `filter_config`; UI knobs only affect manual run → load filter_config in `run_scheduled_pull` or document manual-only [recommend]
- [CONTROL] (Med) `crm_client.py:26-36,80` — cache TTL/retry/circuit via os.getenv at import, not registered → promote to settings or document [defer]
- [CONTROL] (Med) `syncs/detail.html:140-146` + `crm_ticket_pull.py:446` — ambiguous-match `manual_review` unimplemented; mapping_primary/fallback ignored → drop or wire review queue [recommend]
- [POLISH] (Med) `syncs/detail.html:216-221` + `integration_sync.py:228-239` — metrics omit errors/comments/leads/watermark; run w/ per-ticket errors marked success → show counts + watermark, flag partial [recommend]
- [POLISH] (Med) `app/web/admin/integrations.py:74-84` — redrive ignores boolean return, no flash → `?redriven=N`/not-found flash [recommend]
- [POLISH] (Low) `syncs/index.html:87-90`, `detail.html:27-33` — Run Now / re-drive-all no confirm → lightweight confirm [defer]
- [CONTROL] (Low) `crm_webhook.py:32,76` — duplicate JWT cache + hardcoded timeouts, no retry/circuit → route via `CRMClient` [defer]
- Verified: `crm_ticket_pull_enabled`/interval are the canonical scheduler controls; duplicate-merge CLI-only w/ dry-run + `--live`.

### Identity normalization/resolution + CRM customer/portal/billing-push
- [CONTROL] (High) `crm_billing_push.py:54` — snapshot currency via `os.getenv`, bypassing `billing.default_currency` → read from settings [recommend]
- [CONTROL] (High) `customer_identity_normalization.py:19,30` — `DEFAULT_COUNTRY_CODE="234"` hardcoded for all phone canon (setting exists, unused) → make it a setting (default 234) [recommend]
- [POLISH] (High) `crm_customers.py:177-206` — webhook overwrites name/email/phone/address/status with no audit/changelog (merge hazard) → emit audit event (old→new per field) [recommend]
- [CONTROL] (Med) `customer_identity_resolution.py:84-85,159` — sensitive-automation gate hardcodes `{HIGH,MEDIUM}` → min-confidence setting (default MEDIUM) [recommend]
- [POLISH] (Med) `crm_customers.py:145-174` — match decision (matched-via/created-new) not logged → log matched_via + field [recommend]
- [CONTROL] (Med) `crm_customers.py:158-172` — fuzzy-match phone exact-string (no normalize) + mandatory name equality → reuse `normalize_phone_identifier`; make require-name toggle a setting [recommend]
- [POLISH] (Med) `crm_portal.py:508-533` — `reseller_open_tickets_count` returns 0 on CRM error (looks like genuine 0) → propagate "unavailable" [defer]
- [CONTROL] (Med) `crm_billing_push.py:60` — nightly push has no enable/disable/direction control → `crm.billing_push_enabled` flag (default on) [defer]
- [POLISH] (Low) `crm_billing_push.py:90-116` — logs nothing when 0 subscribers CRM-linked (broken link looks like no-op) → log considered/enqueued + warn on 0 [defer]
- [POLISH] (Low) `customer_identity_resolution.py:889-931` — ambiguous multi-subscriber collisions log only count, not ids → include conflicting subscriber_ids [defer]
