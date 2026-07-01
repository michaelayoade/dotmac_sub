# Notifications & messaging — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) channels/senders/suppression
(email/SMS/WhatsApp, queue drain, status/reclaim policy), (b) templates/renderer/
policy + admin notifications UI + alert-policies.
**Status:** remediated for required P0/P1/P2 items on
`codex/notifications-ux-polish-remediation`. Part of the remaining-module audit
series; companion to the networking/billing/catalog audits under `docs/designs/`.

## What this audit is

Two tracks (full definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** (make
existing features feel finished/trustworthy) and **CONTROL** (expose hardcoded
policy as settings/options).

> Operational context: the notification **queue runner has historically been
> OFF** (large SMS backlog). The required queue-safety prerequisites from this
> audit now have code support: SMTP/SMS timeouts, configurable retries/backoff,
> and per-channel rate caps. Re-enabling the runner is still an operational
> decision and should use conservative settings at first.

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

## Remediation status

All required P0, P1, and P2 audit priorities have been implemented in code:

- Template activation now behaves as an actual kill-switch: the form can
  deactivate templates, missing/inactive templates suppress event sends instead
  of falling back to hardcoded copy, and templates have an inline status toggle.
- Customer-facing rendering now fails closed when unresolved variables remain on
  email, SMS, WhatsApp, event-driven sends, and bulk sends.
- Sender reliability controls are configurable: SMTP timeout, SMS provider
  timeout, max retries, sending reclaim timeout, retry backoff, per-channel queue
  cap, SMS max length, and Africa's Talking missing-username handling.
- Operators can preview unsaved templates, confirm live tests and activation
  changes, and preview bulk-send counts before committing rows.
- Delivery policy moved from hardcoded-only behavior to configurable event
  enable/routing settings, per-category preferences, quiet-hours scheduling,
  dedupe windows, and idempotency-key handling.
- Admin lists now expose template search/status filters, alert-policy filters,
  inline toggles, count-backed pagination, generic bulk error copy, and Redis
  client reuse for WebSocket notification publishing.

Required left to mark this audit complete: **none**.

Optional follow-up, outside required P0/P1/P2 completion: add client-side template
lint before submit and expose queue batch-size/reclaim category controls only if
operations later needs them.

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
| **P0** | **Complete.** Active toggle fixed; inactive/missing templates suppress automated sends; unresolved-variable guard covers non-email channels; real SMTP sends have a timeout. |
| **P1** | **Complete.** Per-event enable/routing settings, retry backoff, per-channel rate caps, preview/confirm flows, SMS truncation policy, and Africa's Talking missing-username failure are implemented. |
| **P2** | **Complete.** Quiet-hours, dedupe, idempotency handling, per-category preferences, provider timeout settings, template/alert-policy filters, category single-source, email preview fidelity, count-backed pagination, generic error copy, and Redis client reuse are implemented. |

## Appendix — full findings

Format: `[POLISH|CONTROL] (severity) file:line — problem → recommendation [recommend|defer]`

### Channels / senders / suppression
- [POLISH] (High) `app/services/email.py:861-865` — **resolved**: real SMTP send path now uses a bounded timeout.
- [CONTROL] (High) `app/tasks/notifications.py:308-317` — **resolved**: failed sends are retried with configurable backoff and per-channel queue caps.
- [CONTROL] (Med) `app/tasks/notifications.py:32-33` — **resolved**: max retries and sending-timeout policy are settings-backed.
- [POLISH] (Med) `app/services/sms.py:360-371` + `web_customer_actions.py:809-831` — **resolved**: unresolved variables are blocked for SMS/WhatsApp/template sends and bulk sends.
- [CONTROL] (Med) `app/services/sms.py:86,136,184` — **resolved**: SMS provider timeout is configurable with `sms_api_timeout_seconds`.
- [POLISH] (Med) `app/services/notification_adapter.py:374-384` — **resolved**: truncation moved to the SMS service, logs/uses a configurable `sms_max_length`, and no longer silently trims in the adapter.
- [POLISH] (Med) `app/services/sms.py:277-287` — **resolved**: Africa's Talking no longer defaults missing username to `"sandbox"`.
- [CONTROL] (Med) `notification.py`/`customer_notification_policy.py`/`tasks/notifications.py` — **resolved**: quiet-hours, dedupe windows, and idempotency-key handling are implemented.
- [POLISH] (Med) `app/web/admin/customers.py:1965-1982` — **resolved**: bulk send supports preview/count mode without creating rows, then requires confirmation before commit.
- [POLISH] (Low) `customers.py:1962,1982` — **resolved**: bulk endpoints log exceptions and return generic operator-safe copy.
- [POLISH] (Low) `app/services/notification_adapter.py:529-558` — **resolved**: WebSocket/operation notification publishing reuses the shared Redis client.
- [CONTROL] (Low) `app/tasks/notifications.py:50-51,111` — reclaim category sets + `batch_size=50` hardcoded → document; expose only if needed [defer]
- Verified: single Celery drain with status-gate/suppression/queue-age/reclaim; SMTP senders + activity routing + WhatsApp timeout already configurable; status-policy walled set matches emitted categories.

### Templates / renderer / policy / admin-UI / alert-policies
- [POLISH] (High) `app/web/admin/notifications.py:225` — **resolved**: inactive checkbox posts correctly and can deactivate templates.
- [CONTROL] (High) `app/services/events/handlers/notification.py:563-574` — **resolved**: deactivated/missing templates suppress sends instead of falling back to in-code spec text.
- [CONTROL] (High) `notification.py:78-449` — **resolved**: per-event enable and per-event channel routing settings override spec defaults.
- [CONTROL] (Med) `app/services/customer_notification_policy.py:43-53` — **resolved**: per-category preference keys and quiet-hours support are implemented.
- [POLISH] (Med) `templates/admin/notifications/template_form.html:151,178-187` — **resolved for required scope**: unsaved templates can be previewed before first save; optional client-side lint remains a future nicety because server-side validation is still authoritative.
- [POLISH] (Med) `template_form.html:89-101` + `notifications.py:271-305` — **resolved**: activation changes and live test sends require confirmation.
- [POLISH] (Med) `templates/admin/notifications/templates_list.html:37-51` — **resolved**: search, status filter, and inline toggle are available.
- [CONTROL] (Med) `app/services/web_notifications_alert_policies.py:36-46` + `notifications.py:444-453` — **resolved**: channel/status/severity/active filters are exposed.
- [CONTROL] (Low) `customer_notification_policy.py:21-30` — **resolved**: category resolution now uses `EVENT_NOTIFICATION_SPECS` as the source of truth before fallback prefixes.
- [POLISH] (Low) `templates/admin/notifications/_template_preview.html:8` — **resolved**: email previews render HTML bodies with delivered-view fidelity.
- [CONTROL] (Low) `web_notifications_alert_policies.py:48-59,259-267` — **resolved**: alert-policy pagination uses a count query.
- Verified: one render contract (single-brace, save-time validated blocking `{{}}`/unknown vars); alert severity options match `AlertSeverity`.
