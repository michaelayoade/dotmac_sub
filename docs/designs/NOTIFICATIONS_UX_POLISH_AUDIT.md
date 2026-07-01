# Notifications & messaging — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) channels/senders/suppression
(email/SMS/WhatsApp, queue drain, status/reclaim policy), (b) templates/renderer/
policy + admin notifications UI + alert-policies.
**Status:** audit only. Part of the remaining-module audit series; companion to the
networking/billing/catalog audits under `docs/designs/`.

## What this audit is

Two tracks (full definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** (make
existing features feel finished/trustworthy) and **CONTROL** (expose hardcoded
policy as settings/options).

> ⚠️ Operational context: the notification **queue runner has historically been
> OFF** (large SMS backlog). Several P0/P1 items below (retry backoff, per-channel
> rate limit, SMTP timeout) are **prerequisites to safely re-enabling sending** —
> do not re-enable the runner before they land.

## Acceptance criteria (notifications-specific)

1. A control that says "off"/"inactive" actually stops the send — no in-code
   fallback keeps sending a deactivated template.
2. No unresolved template variable can reach a customer on *any* channel
   (email guard must extend to SMS/WhatsApp).
3. Outbound senders have timeouts, bounded retries with backoff, and a per-channel
   rate cap.
4. Mass/automated sends are previewable (resolved count, rendered body) and
   confirmed before dispatch.
5. Delivery policy (which events notify, on which channels, with what opt-outs) is
   operator-configurable, not hardcoded.

## Cross-cutting themes

### POLISH

**P-A. Misleading / broken controls** (the recurring signature).
- Template **Active toggle is un-uncheckable**: `is_active: bool = Form(True)` — an
  unchecked box is omitted by the browser → falls back to `True`; a template can
  never be deactivated via the form (`app/web/admin/notifications.py:225`)
- Even when a template is **deactivated/missing, the event still sends** using the
  in-code `spec` subject/body fallback — toggling "Inactive" does not stop
  automated customer sends (`app/services/events/handlers/notification.py:563-574`)

**P-B. No preview / confirm before mass or automated sends.**
- Preview/Send-Test gated behind `{% if template %}` — a new template can't be
  previewed before its first save (`templates/admin/notifications/template_form.html:151`)
- No confirm before activating a template that feeds all-customer sends; "Send Test
  Message" dispatches a real live message (`dry_run=False`) with no confirm (`notifications.py:271-305`)
- Bulk send commits immediately on POST; the matched/queued/suppressed breakdown
  only returns *after* the send — no dry-run preview-count first (`app/web/admin/customers.py:1965-1982`)

**P-C. Send reliability / customer-content correctness.**
- Real SMTP send path has **no socket timeout** — a hung server wedges the worker
  (only `test_smtp_connection` passes one) (`app/services/email.py:861-865`)
- **SMS/WhatsApp template substitution doesn't validate placeholders** — an
  unfilled `{{var}}` leaks to the customer; the guard exists for email only
  (`app/services/sms.py:360-371`, `app/services/web_customer_actions.py:809-831`) — the documented literal-leak class, still open on non-email channels
- SMS silently truncated to 160 chars, no multipart/warning (`app/services/notification_adapter.py:374-384`)
- Africa's Talking `username` defaults to `"sandbox"` when unset → prod sends
  silently go to sandbox (`app/services/sms.py:277-287`)

**P-D. Observability / feedback / list gaps.**
- Bulk endpoints surface raw exception text via `HTTPException(..., detail=str(e))` (`customers.py:1962,1982`)
- WebSocket/operation-status adapter creates a fresh `redis.from_url` per call,
  never closed (connection churn — the ingestion-OOM lesson) (`app/services/notification_adapter.py:529-558`)
- Template list filters by channel only — no name/code search, no active/inactive
  filter, no inline toggle (`templates/admin/notifications/templates_list.html:37-51`)
- Alert-policy / on-call list passes filters as `None`, no UI controls though the
  service supports them (`app/services/web_notifications_alert_policies.py:36-46`)

### CONTROL

**C-1. Delivery policy hardcoded** (the headline gap).
- Which events notify + channel routing fully hardcoded in
  `EVENT_NOTIFICATION_SPECS` (`app/services/events/handlers/notification.py:78-449`);
  only SMS has an enable flag. No per-event/per-channel kill-switch → operators
  can't silence a noisy event or add/drop a channel without a code change.
  → per-event-code enable + per-channel routing as settings/DB rows, current specs
  as defaults.

**C-2. Send-reliability policy hardcoded.**
- Failed notifications re-picked every ~1 min with **no backoff** and **no
  per-channel rate cap** (`app/tasks/notifications.py:308-317`) — a burst loop
  against the provider on re-enable
- `MAX_RETRIES=3`, `SENDING_TIMEOUT_MINUTES=10` module constants (queue-age already
  a setting) (`app/tasks/notifications.py:32-33`)
- SMS provider timeouts hardcoded `30.0` while WhatsApp reads
  `whatsapp_api_timeout_seconds` (inconsistent) (`app/services/sms.py:86,136,184`)

**C-3. Missing customer controls / dedupe.**
- No quiet-hours, no dedupe/cooldown window, `NotificationRequest.idempotency_key`
  exists but is unused (no true exactly-once) (`app/services/notification.py`, `tasks/notifications.py`)
- Customer opt-out is only two `subscriber.metadata_` flags (billing/sms), both
  default True, no per-category or quiet-hours (`app/services/customer_notification_policy.py:43-53`)

**C-4. Duplicated constant (drift).**
- Event→category mapping uses hardcoded prefix tuples
  (`customer_notification_policy.py:21-30`) duplicating event naming; a new prefix
  silently falls to `"general"` → derive category from
  `EVENT_NOTIFICATION_SPECS[...].category` (single source of truth).

## Priority

| Tier | Items |
|------|-------|
| **P0** | Active-toggle un-uncheckable (`notifications.py:225`); deactivated template still sends via spec fallback — the kill-switch doesn't kill (`notification.py:563-574`); SMS/WhatsApp unresolved-variable leak to customers (`sms.py:360`); SMTP send has no timeout → worker wedge (`email.py:861`) |
| **P1** | Per-event/per-channel enable + routing as settings (C-1, the real kill-switch); retry backoff + per-channel rate cap (C-2) **before re-enabling the runner**; preview/confirm before activate + mass-send + live test (P-B); SMS truncation + AT sandbox-default (P-C) |
| **P2** | quiet-hours/dedupe/idempotency (C-3), opt-out per-category, provider timeouts as settings, template/alert-policy list filters, category single-source (C-4), preview HTML fidelity, pagination count queries, raw-exception copy, redis client reuse |

## Appendix — full findings

Format: `[POLISH|CONTROL] (severity) file:line — problem → recommendation [recommend|defer]`

### Channels / senders / suppression
- [POLISH] (High) `app/services/email.py:861-865` — real SMTP send path no `timeout` (only test path has one); hung server wedges worker → plumb socket timeout (reuse `smtp_test_timeout_seconds` or new key) [recommend]
- [CONTROL] (High) `app/tasks/notifications.py:308-317` — failed notifications re-picked ~1 min, no backoff, no per-channel rate cap → configurable backoff (1/5/15) + rate cap [recommend]
- [CONTROL] (Med) `app/tasks/notifications.py:32-33` — `MAX_RETRIES=3`/`SENDING_TIMEOUT_MINUTES=10` constants → settings (1-10 / 2-60) [recommend]
- [POLISH] (Med) `app/services/sms.py:360-371` + `web_customer_actions.py:809-831` — SMS/WhatsApp template subst not validated; literal `{{var}}` leaks (email guarded) → apply unresolved-var guard to SMS/WhatsApp + `send_with_template` [recommend]
- [CONTROL] (Med) `app/services/sms.py:86,136,184` — provider HTTP timeouts hardcoded `30.0` while WhatsApp configurable → `sms_api_timeout_seconds` (default 30, 5-60) [recommend]
- [POLISH] (Med) `app/services/notification_adapter.py:374-384` — SMS silently truncated to 160, no multipart/warning → log/flag + configurable limit / concatenated SMS [recommend]
- [POLISH] (Med) `app/services/sms.py:277-287` — Africa's Talking username defaults `"sandbox"`; prod sends silently sandboxed → treat missing username as config error for non-sandbox keys [recommend]
- [CONTROL] (Med) `notification.py`/`customer_notification_policy.py`/`tasks/notifications.py` — no quiet-hours, no dedupe window, `idempotency_key` unused → quiet-hours + dedupe settings (off by default) before automated blasts [defer]
- [POLISH] (Med) `app/web/admin/customers.py:1965-1982` — bulk send commits immediately; no dry-run/preview-count → preview mode returning counts, no rows [recommend]
- [POLISH] (Low) `customers.py:1962,1982` — bulk endpoints return raw `str(e)` → log + generic message [defer]
- [POLISH] (Low) `app/services/notification_adapter.py:529-558` — WebSocket/operation publish create fresh redis client per call, never closed → reuse/close (mirror operation_notifications) [recommend]
- [CONTROL] (Low) `app/tasks/notifications.py:50-51,111` — reclaim category sets + `batch_size=50` hardcoded → document; expose only if needed [defer]
- Verified: single Celery drain with status-gate/suppression/queue-age/reclaim; SMTP senders + activity routing + WhatsApp timeout already configurable; status-policy walled set matches emitted categories.

### Templates / renderer / policy / admin-UI / alert-policies
- [POLISH] (High) `app/web/admin/notifications.py:225` — `is_active: bool = Form(True)` → template can't be deactivated via form → use `str | None = Form(None)` pattern [recommend]
- [CONTROL] (High) `app/services/events/handlers/notification.py:563-574` — deactivated/missing template still queues via in-code spec fallback → per-event-code enable honored before queueing [recommend]
- [CONTROL] (High) `notification.py:78-449` — event→channel routing hardcoded in `EVENT_NOTIFICATION_SPECS` (only SMS has a flag) → per-event-code channel settings, specs as defaults [recommend]
- [CONTROL] (Med) `app/services/customer_notification_policy.py:43-53` — opt-out only 2 metadata flags (default True), no quiet-hours, no service/account/usage opt-out → per-category prefs + quiet-hours [defer]
- [POLISH] (Med) `templates/admin/notifications/template_form.html:151,178-187` — preview/test gated behind saved template; validation server-side only → allow preview on new + client-side lint mirroring `validate_template_text` [recommend]
- [POLISH] (Med) `template_form.html:89-101` + `notifications.py:271-305` — no confirm before activate (all-customer sends) or live test send (`dry_run=False`) → confirms [recommend]
- [POLISH] (Med) `templates/admin/notifications/templates_list.html:37-51` — list filters channel-only; no name/code search, no status filter, no inline toggle → add search + status filter + inline toggle (post bug-fix) [recommend]
- [CONTROL] (Med) `app/services/web_notifications_alert_policies.py:36-46` + `notifications.py:444-453` — alert-policy/on-call list passes filters as None, no UI controls → expose channel/status/severity/active filters [defer]
- [CONTROL] (Low) `customer_notification_policy.py:21-30` — category from hardcoded prefix tuples duplicating event naming; new prefix → "general" → derive from `EVENT_NOTIFICATION_SPECS.category` [defer]
- [POLISH] (Low) `templates/admin/notifications/_template_preview.html:8` — body autoescaped so HTML email preview shows escaped tags (differs from delivered) → render email preview as sanitized HTML [defer]
- [CONTROL] (Low) `web_notifications_alert_policies.py:48-59,259-267` — pagination total fetches up to 10000 rows then `len()` → use count query [defer]
- Verified: one render contract (single-brace, save-time validated blocking `{{}}`/unknown vars); alert severity options match `AlertSeverity`.
