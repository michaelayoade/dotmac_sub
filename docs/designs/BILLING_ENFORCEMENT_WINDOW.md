# Billing / dunning enforcement time-of-day window

Status: accepted and active. Owner: billing.

## Problem

Customer-impacting billing actions can fire at the wrong local hour. Daily
runners are anchored to **00:00–05:59 UTC** (`app/services/scheduler_config.py`
`_interval_to_beat_schedule`, hash-spread by task id), so postpaid suspension and
dunning comms can hit customers around ~01:00 UTC (~02:00 WAT). Ops want
enforcement and notifications to happen only at civilized local hours, and to be
able to set when jobs run.

There is **no complete working time-of-day control today** for unified billing
enforcement. `billing_notif_send_hour` is exposed in the admin UI
(`templates/admin/system/config/billing_notifications.html`) but is **inert** —
nothing reads it.

## Canonical contract

`app/services/enforcement_window.py` contains the shared wall-clock resolver
used by billing and collections:

- `scheduler.timezone` (canonical local TZ, default = app TZ)
- `to_local(db, run_at)` — convert to `scheduler.timezone`
- `parse_time(value)` — "HH:MM[:SS]" → `time`
- `window_block_reason(...)` — reason str or `None`

Every calendar day is eligible. The only configurable enforcement timing inputs
are `collections.enforcement_window_start` and
`collections.enforcement_window_end`. Weekends and holidays are not alternate
business states, and there is no audit/enforce runtime mode.

Two distinct timezone concepts remain: celery beat fires on the **celery app
TZ** (currently UTC); in-task decisions use **`scheduler.timezone`**.

## Design — two complementary parts

### Part A — Window guard
A shared, settings-driven gate evaluated *inside* the tasks, so customer-impacting
actions never happen off-hours regardless of trigger (beat / retry / manual
"Run now" / duplicate beat).

- `app/services/enforcement_window.py`:
  - `to_local(db, run_at)` — convert to `scheduler.timezone`.
  - `parse_time(value)` — "HH:MM[:SS]" → `time`.
  - `window_block_reason(local_run_at, *, start_time, end_time)` → reason str or
    `None`. Pure; supports midnight-wrapping windows (e.g. 22:00–06:00).
- `within_send_window(db, now)` gates outbound comms
  (`_emit_invoice_reminders` / `_emit_dunning_escalations` in
  `billing_automation.py`); activates `billing_notif_send_hour`.
- `within_enforcement_window(db, now)` gates state changes (postpaid overdue
  suspend in `events/handlers/enforcement.py` + unified dunning enforcement).

**Cadence prerequisite:** a window is only effective if the task fires often
enough to land inside it. Daily enforcers/notifiers must move to **hourly +
once-per-day idempotency** before their gate is wired — otherwise a daily
01:00-UTC run + a 09:00 window means the action never fires. This is why
notification/enforcement gating is staged after the helper, not shipped with it.

### Part B — Cron scheduling (ops control of run time)
Let admins set the exact run time of any scheduled job.

- Migration: extend `ScheduleType` enum with `crontab` (pg `ALTER TYPE` — apply as
  `postgres`, not `dotmac_app`); add nullable `cron_expr VARCHAR` (5-field
  `m h dom mon dow`) to `scheduled_tasks`.
- `scheduler_config.build_beat_schedule`: when `schedule_type == crontab`, emit
  `crontab(*parse(cron_expr))` (import + daily-anchor crontab already present).
  `DbScheduler` refresh (300s) picks up edits without restart.
- Admin UI (`scheduler_detail.html` + `web_system_scheduler.py`): edit
  type/cron/interval, validate cron, show next-run + active TZ. Also delivers the
  editable interval the UI currently lacks.

### Unifying decision — one timezone
Set the celery app timezone from `scheduler.timezone` (today hardcoded
`CELERY_TIMEZONE`/UTC at `celery_app.py`). Then cron "hour=9" = 9am local, the
daily-anchor window becomes local, and the window-guard (already on
`scheduler.timezone`) is consistent. This is the one deliberately
behavior-affecting step; gate on ops setting `scheduler.timezone` and call it out
in the deploy note (it shifts when existing daily jobs fire).

## Implementation

1. `within_send_window` gates the permanent hourly notification owner
   (`app.tasks.billing.run_billing_notifications`) to its configured local
   delivery window.
2. Migration 168 added crontab scheduling (`crontab` enum value +
   nullable `cron_expr`); `_cron_to_beat_schedule` parses a 5-field cron;
   `build_beat_schedule` honours `schedule_type == crontab` rows. The admin UI
   edits type, cron, cadence, and active timezone.
3. The canonical financial-access preview defers with
   `outside_enforcement_window`; confirmation recomputes and fingerprints the
   same time decision before recording the consequence.
4. Migration 398 removes the former mode, weekend, holiday, and prepaid-specific
   timing rows. It retains only the shared start/end window.

## Risks / rollout

- Enum `ALTER TYPE` migration must run as `postgres` and via `make prod-migrate`
  against the immutable image.
- TZ unification shifts when existing daily jobs fire (UTC→local) — intended;
  document in the deploy note.
- Daily-task-fires-outside-window pitfall — covered by the hourly+idempotent
  switch in phases 2 & 6.
- A narrow window requires a task cadence that enters it. Permanent lifecycle
  tasks remain scheduled and idempotent; operators may change cadence or local
  time, but cannot disable the owner.
