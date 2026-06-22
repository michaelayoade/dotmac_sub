# Billing / dunning enforcement time-of-day window

Status: in progress (Phase 1 landed). Owner: billing.

## Problem

Customer-impacting billing actions can fire at the wrong local hour. Daily
runners are anchored to **00:00–05:59 UTC** (`app/services/scheduler_config.py`
`_interval_to_beat_schedule`, hash-spread by task id), so postpaid suspension and
dunning comms can hit customers around ~01:00 UTC (~02:00 WAT). Ops want
enforcement and notifications to happen only at civilized local hours, and to be
able to set when jobs run.

There is **no working time-of-day control today** except for the prepaid
enforcement path. `billing_notif_send_hour` is exposed in the admin UI
(`templates/admin/system/config/billing_notifications.html`) but is **inert** —
nothing reads it.

## What already exists (the pattern we generalize)

`PrepaidEnforcement.run()` (`app/services/collections/_core.py`) already gates on
a wall-clock window:

- `scheduler.timezone` (canonical local TZ, default = app TZ)
- `collections.prepaid_blocking_time` ("HH:MM", default `08:00`) — skip if local
  time is before it
- `collections.prepaid_skip_weekends`, `collections.prepaid_skip_holidays`
- once-per-day idempotency via `collections.prepaid_last_run_date`
- fires **hourly** (`prepaid_enforcement_interval_seconds`, default 3600) and the
  in-task gate makes it *act once*, after the local hour.

This is the proven design. Two distinct timezone concepts: celery beat fires on
the **celery app TZ** (currently UTC); in-task decisions use **`scheduler.timezone`**.

## Design — two complementary parts

### Part A — Window-guard (in-task safety net)
A shared, settings-driven gate evaluated *inside* the tasks, so customer-impacting
actions never happen off-hours regardless of trigger (beat / retry / manual
"Run now" / duplicate beat).

- `app/services/enforcement_window.py` (Phase 1, landed):
  - `to_local(db, run_at)` — convert to `scheduler.timezone`.
  - `parse_time(value)` — "HH:MM[:SS]" → `time`.
  - `window_block_reason(local_run_at, *, start_time, end_time, skip_weekends,
    skip_holidays)` → reason str or `None`. Pure; supports midnight-wrapping
    windows (e.g. 22:00–06:00).
- `within_send_window(db, now)` gates outbound comms
  (`_emit_invoice_reminders` / `_emit_dunning_escalations` in
  `billing_automation.py`); activates `billing_notif_send_hour`.
- `within_enforcement_window(db, now)` gates state changes (postpaid overdue
  suspend in `events/handlers/enforcement.py` + `DunningRun`; prepaid already).

**Cadence prerequisite:** a window is only effective if the task fires often
enough to land inside it. Daily enforcers/notifiers must move to **hourly +
once-per-day idempotency** (mirror prepaid's `last_run_date`) before their gate
is wired — otherwise a daily 01:00-UTC run + a 09:00 window means the action
never fires. This is why notification/enforcement gating is staged after the
helper, not shipped with it.

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

## Phases

1. **Window helper + tests** — `enforcement_window.py`; refactor prepaid to use it
   (byte-equivalent). *(landed; no behavior change.)*
2. **Notification gating** — hourly billing-notifications cadence (flag, default
   off) + `within_send_window` on the emit functions; activate
   `billing_notif_send_hour`.
3. **Cron model + scheduler** — migration, enum, `build_beat_schedule` branch.
4. **Cron admin UI** — edit type/cron/interval + next-run preview.
5. **Unify timezone** — celery app TZ ← `scheduler.timezone`.
6. **Enforcement gating** — postpaid suspend/dunning hourly + once-per-day +
   `within_enforcement_window`; **audit-first** (log "would-gate" before
   enforcing).

## Risks / rollout

- Defaults preserve current behavior; deploy = no change until settings are set.
- Enum `ALTER TYPE` migration must run as `postgres` and via `make prod-migrate`
  against the immutable image.
- TZ unification shifts when existing daily jobs fire (UTC→local) — intended;
  document in the deploy note.
- Daily-task-fires-outside-window pitfall — covered by the hourly+idempotent
  switch in phases 2 & 6.
- Enforcement changes land audit-first.
