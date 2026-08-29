# NCC weekly report cutover — CRM off, Sub on

**Status: LIVE REGULATORY GAP.** CRM/Omni was decommissioned on 2026-08-29 and
Selfcare's `ncc_report_email_enabled` is still `false`, so the Tuesday NCC
complaints workbook is currently **not being produced by anything**. This is no
longer a coordinated hand-off between two live senders. It is the closure of an
outage in a regulatory obligation, and it has **no fallback** — see
[No fallback exists](#no-fallback-exists).

Owner of the delivery: `communications.ncc_weekly_delivery`
(`app/services/ncc_report_email.py`), SOT migration state `shadowing`.
Owner of the report projection: `compliance.ncc_complaints_reporting`
(`app/services/ncc_complaints_report.py`), already `cut_over`.
Design of record: `docs/designs/NCC_WEEKLY_REPORT_DELIVERY.md`.

---

## No fallback exists

Every earlier revision of this runbook assumed CRM could be re-enabled. It
cannot. On 2026-08-29 the CRM/Omni runtime on the named host was destroyed:
all containers removed, **all ten volumes deleted** — including the production
database and the volume holding the previously generated NCC weekly reports —
images removed, the vhost and its certificate removed, the backup units
removed, and the deployment checkout shredded.

Consequences to plan around rather than discover:

- **There is no CRM sender to disable.** The old "disable the CRM scheduled
  sender" step is already satisfied by the decommission and needs no action.
- **There is no CRM sender to fall back to.** If Selfcare delivery fails, the
  remedy is to repair Selfcare or to file manually from the preserved
  artifact. Re-enabling CRM is not an option at any point in this runbook.
- **The CRM configuration can no longer be exported from a running system.**
  The only surviving copy of CRM data is an off-host database dump taken
  2026-08-25 (`dotmac_omni_2026-08-25_180002.sql.gz` on the shared backup
  remote). Recovering the configuration means restoring that dump to a
  disposable database — never onto a fleet host — or reconstructing the
  configuration from operator knowledge and having Michael confirm the
  recipients.
- **"Never leave CRM and Selfcare enabled together" is now vacuously true.**
  Keep the sentence in mind only if a CRM restore is ever performed for
  forensic reasons; such a restore must never be given mail credentials.

---

## What must be set, and exactly where

There is **no feature-flag service** for this. The gate and all ten
configuration values are rows in the generic `domain_settings` table, domain
`notification`, written through the typed owner.

| Setting key | Purpose | Spec default |
|---|---|---|
| `ncc_report_email_enabled` | the gate | `false` |
| `ncc_report_email_to` | primary recipient | `""` |
| `ncc_report_email_cc` | cc recipients | `""` |
| `ncc_report_email_bcc` | bcc recipients | `""` |
| `ncc_report_email_sender_key` | sending identity | `""` |
| `ncc_report_email_subject` | subject | `"Weekly NCC Report"` |
| `ncc_report_email_body_template` | body | four-line default |
| `ncc_report_email_local_time` | send time | `"08:00"` |
| `ncc_report_email_timezone` | schedule timezone | `"Africa/Lagos"` |
| `ncc_report_email_send_day` | send day | `"tuesday"` |
| `ncc_report_email_lookback_days` | report window | `7` |

Specs: the `notification` block of `app/services/settings_spec.py`.
Key constants: `app/services/ncc_report_email.py`.

### This is a configuration change, not a code change

No deploy is required to turn delivery on. Two write paths exist, and only two:

1. **Admin form — the production path.**
   `POST /admin/reports/ncc-email-settings`, rendered by
   `GET /admin/reports/ncc-complaints` (`app/web/admin/reports.py`; template
   `templates/admin/reports/ncc_complaints.html`).
   Permission `notification:write` to save, `reports:ncc:read` to view.
   Form fields: `enabled`, `recipient`, `cc`, `bcc`, `sender_key`, `subject`,
   `body_template`, `local_time`, `timezone`, `send_day`, `lookback_days`.
   Note the field is **`recipient`**, not `to`.
2. **The importer script**, for recovery and comparison — see Step 1.

There is **no JSON/REST API** for this configuration.

### Environment variables will NOT work — read this before trying

Each spec declares an `NCC_REPORT_EMAIL_*` env var, and ten of the eleven are
**inert at runtime**:

- `seed_scheduler_runtime_settings` (`app/services/settings_seed.py`) is the
  only reader of these env vars, it runs once at seeding, and it iterates
  `SCHEDULER_BOOLEAN_SETTING_KEYS | SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS`.
  Of the eleven NCC keys, only `ncc_report_email_enabled` appears in either
  set (`SCHEDULER_BOOLEAN_SETTING_KEYS`, `app/services/settings_spec.py`).
- Therefore **setting `NCC_REPORT_EMAIL_TO` in the environment does nothing.**
  Recipients exist only as a `domain_settings` row.
- Even for the gate, `ensure_by_key` **preserves an existing operator
  decision**. The row already exists, seeded `false`, so changing
  `NCC_REPORT_EMAIL_ENABLED` and restarting will not flip it.

**So the production enable is the admin form — not an env var, not a
redeploy.** Anyone who "sets the env var and restarts" will observe no change
and may wrongly conclude the feature is broken.

### Turning it on takes effect within about five minutes, with no beat restart

The admission poll is registered conditionally in
`app/services/scheduler_config.py`:

```python
if ncc_report_email_enabled:
    schedule["ncc_report_email"] = {
        "task": "app.tasks.reports.send_scheduled_ncc_report",
        "schedule": timedelta(minutes=5),
    }
```

`app/celery_scheduler.py` rebuilds the beat schedule from the database every
`scheduler.beat_refresh_seconds` (spec default **300 s**). The entry therefore
appears — and disappears — on its own. Do not restart beat as part of this
change; a restart has a larger blast radius than the change itself.

---

## Understand what the first occurrence will and will not cover

`run_due_delivery` (`app/services/ncc_report_email.py`) computes:

```
end   = the scheduled local instant  (send_day at local_time, in timezone)
start = end - lookback_days
```

with `lookback_days = 7` by default. The report is a **rolling seven-day
window ending at the send instant** — not a calendar week, and **not a
backfill**.

- Enabling on a Tuesday **after** `local_time` queues that Tuesday's
  occurrence on the next poll, covering only the previous seven days.
- Enabling on any other day, or before `local_time`, returns
  `not_scheduled_day` / `before_scheduled_time` and waits.
- **Weeks between CRM's last send and Sub's first send are silently missing.**
  Enabling does not recover them. Each missing week is a separate remediation
  decision — see [Step 5](#step-5--remediating-the-missed-weeks).

Establish the last date CRM actually delivered **before** enabling, so the size
of the gap is known. Sources, in order of preference: the recipients' own
mailboxes; the restored 2026-08-25 dump.

---

## Step 1 — Recover and validate the configuration

The importer is dry-run by default and never prints recipient addresses:
`scripts/migration/migrate_ncc_weekly_report_config.py`.

Input JSON keys: `enabled`, `to`, `cc`, `bcc`, `sender_key`, `subject`,
`body_template`, `local_time`, `timezone`, `send_day`, `lookback_days`.

```bash
# Dry run: validates only, opens no database session, writes nothing.
python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json

# Apply, through the typed owner, on STAGING only.
python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json --apply
```

The dry run reports `"mode": "dry-run"` and a redacted preview:
`primary_recipient_configured` (a boolean), `cc_recipient_count`,
`bcc_recipient_count`, `sender_key`, `subject`, `local_time`, `timezone`,
`send_day`, `lookback_days`, `body_template_sha256`. Recipient addresses are
never printed — do not defeat that by pasting the JSON into a ticket, a pull
request, or a chat message.

Checks that stop the cutover if they fail:

- `send_day` is `"tuesday"`.
- `primary_recipient_configured` is `true` — an empty `to` yields the
  `missing_recipient` decision and no report is ever produced.
- `body_template_sha256` matches the body intended to go out. Only these
  placeholders are permitted: `download_url`, `lookback_days`,
  `not_filable_count`, `report_date`, `row_count`.
- `timezone` is `"Africa/Lagos"` and `local_time` is the intended send time.

Keep `crm-ncc.json` in a protected local location. It contains recipient
addresses. Delete it once the cutover is accepted.

---

## Step 2 — Staging acceptance: the shadow-verification step

Sub has **no runtime shadow or no-send mode.** There is no `dry_run`,
`shadow` or `no_send` switch anywhere in the delivery path; `enabled=false` is
the only "off". The only honest shadow verification is therefore a real
delivery on staging to a controlled recipient.

1. Apply the configuration to staging with `--apply`, `enabled` still `false`.
2. Review every effective field on `/admin/reports/ncc-complaints`.
3. Generate the NCC complaints export manually and check its content against
   what the regulator expects.
4. Set the staging recipient to a controlled internal address, set `send_day`
   to today and `local_time` to a few minutes ahead, and enable.
5. Observe exactly one occurrence and one queued notification. Verify workbook
   contents, filename, To/CC/BCC, sender identity, subject and body, the
   run-history row, and the artifact download.
6. Restore `send_day` to `tuesday` and the real `local_time`, and disable
   staging again unless production promotion follows immediately.

Do not skip step 4 because the code "is already tested". What is being verified
here is the *configuration and the mail path*, which no test covers.

---

## Step 3 — Production enable: the cutover gate

**Gate — all four must hold before the switch is flipped:**

1. Michael has named the production target and explicitly authorized the work.
2. Staging acceptance (Step 2) produced one delivered notification and a
   downloadable artifact whose SHA-256 matched.
3. The configuration staged in production has been compared field by field
   against the accepted staging configuration, with `enabled` still `false`.
4. The size of the missed-week gap is known and a decision has been recorded
   for it (Step 5).

Then:

1. Apply the reviewed configuration in production with `enabled` **false**.
2. Compare all effective fields on `/admin/reports/ncc-complaints`.
3. Set `enabled` **true** via the admin form.
4. Within about five minutes, confirm the `ncc_report_email` beat entry is
   registered and the five-minute poll is running.
5. On the next due Tuesday, confirm **exactly one** run and **exactly one**
   queued notification — Step 4.

---

## Step 4 — Prove a report was actually delivered

"The task ran" is not proof. "A run row exists" is not proof either — a run row
can be `failed`. Delivery is proven by five durable records that must all
agree.

### The occurrence

Table `ncc_weekly_report_runs` (model `NccWeeklyReportRun`,
`app/models/ncc_reporting.py`; migration `533_ncc_weekly_report_delivery`).

```sql
SELECT id, scheduled_local_date, status, row_count, not_filable_count,
       artifact_sha256, notification_id, failure_code
FROM   ncc_weekly_report_runs
WHERE  schedule_key = 'ncc_complaints'
  AND  scheduled_local_date = DATE '<the Tuesday>';
```

- `status` is one of exactly two values: `queued` or `failed`. There is **no
  `delivered` occurrence state** — delivered-ness lives on the notification.
- `UNIQUE (schedule_key, scheduled_local_date)` is what makes a duplicate send
  structurally impossible for a given local date.
- A `queued` row is required by check constraint to carry `artifact_content`,
  `artifact_sha256` and `notification_id`.

### The delivery

```sql
SELECT n.id, n.channel, n.status, n.sent_at
FROM   notifications n
JOIN   ncc_weekly_report_runs r ON r.notification_id = n.id
WHERE  r.schedule_key = 'ncc_complaints'
  AND  r.scheduled_local_date = DATE '<the Tuesday>';
```

**`notifications.status = 'delivered'` with a non-null `sent_at` is the proof
of delivery.** `queued` means only that it was handed to the mail path.

### The intent

Table `communication_intents`, `dedupe_key = 'ncc-weekly:<YYYY-MM-DD>'`,
`event_type = 'ncc.weekly_report.ready'`, `category = 'regulatory'`. The unique
index on `dedupe_key` is the second, independent duplicate guard.

### The audit trail

Table `audit_events`, `entity_type = 'ncc_weekly_report_run'`:

- `ncc.weekly_report_queued` — metadata carries `scheduled_local_date`,
  `row_count`, `not_filable_count`, `artifact_sha256`.
- `ncc.weekly_report_failed` — `is_success = false`, metadata carries
  `failure_code`.
- `ncc.weekly_delivery_configuration_changed` — every configuration edit,
  including the enable itself. Expect exactly one at cutover.

### The artifact

Download the exact bytes that were attached:
`GET /admin/reports/ncc-weekly-runs/{run_id}/download` (permission
`reports:ncc:export`). The route re-verifies the stored SHA-256 and raises
`artifact_integrity_failed` on mismatch, so a successful download is itself an
integrity check. Record the digest alongside the run id in the cutover record.

### The operator view

`/admin/reports/ncc-complaints`, section **"Recent scheduled delivery
evidence"**, shows the newest runs with their joined delivery status. It is
capped at 50 rows and is **not** keyed by date — for an arbitrary past date,
use the SQL above.

### Delivery evidence checklist

For the first production Tuesday, record all of:

- [ ] exactly one `ncc_weekly_report_runs` row for that `scheduled_local_date`,
      `status = 'queued'`
- [ ] its `notifications` row `status = 'delivered'`, `sent_at` populated
- [ ] exactly one `communication_intents` row with
      `dedupe_key = 'ncc-weekly:<date>'`
- [ ] one `ncc.weekly_report_queued` audit row, `is_success = true`
- [ ] artifact downloaded, SHA-256 recorded and matching `artifact_sha256`
- [ ] `row_count` plausible against the manual complaints export
- [ ] the recipient confirms receipt out of band

---

## Step 5 — Remediating the missed weeks

Enabling covers the current week only. For each Tuesday between CRM's last
delivery and Sub's first, decide and record one of:

- **File late.** `run_due_delivery` accepts an explicit `observed_at` on
  `RunNccWeeklyDeliveryCommand`, so a past occurrence can be generated with the
  correct window. **No checked-in entry point exposes this** — the Celery task
  always passes `datetime.now(UTC)`. Doing it therefore requires an approved
  one-off operator action against the typed owner, executed under the same
  authorization as any other production work, and it will create a real
  occurrence row and send a real email.
- **Regenerate without sending.** Produce the workbook from
  `compliance.ncc_complaints_reporting` for the historical window and file it
  through whatever channel the regulator accepts, creating no occurrence.
- **Accept and disclose.** Record the gap and notify the regulator.

Whichever is chosen, write the decision and its authorization into the
retirement ledger. An unremediated silent gap in a regulatory return is the
worst of the three outcomes and must not become the default by inaction.

---

## Rollback

There is no CRM to roll back to. The only rollback is to stop Selfcare
sending, which **reopens the regulatory gap** — so it is a deliberate,
recorded decision, not a reflex.

- **Before any occurrence is queued:** set `enabled` false via the admin form.
  The beat entry disappears within about five minutes. Record the run
  `failure_code` if one was written.
- **After an occurrence is queued for a local date:** do not attempt to resend
  that date. The `(schedule_key, scheduled_local_date)` unique constraint and
  the `communication_intents.dedupe_key` index will both refuse a duplicate,
  and a duplicate regulatory email is worse than a late one. Repair or
  redeliver from the **preserved artifact** under an approved operator action.
- Rollback never deletes run evidence or the queued artifact.
- If the workbook content is wrong, fix the configuration or the projection and
  file a correction. Disabling the sender does not un-file a report.

---

## Retirement follow-through

After the first accepted production Tuesday:

1. Move `communications.ncc_weekly_delivery` in the SOT registry from
   `AuthorityMigrationState.SHADOWING` to `CUT_OVER`
   (`app/services/sot_registry/domains/notifications_communications.py`) and
   regenerate `docs/SOT_RELATIONSHIP_MAP.md` in the same change.
2. Update the CRM retirement ledger
   (`docs/audits/crm_web_retirement_ledger.json`) with the run id, the local
   date and the artifact digest.
3. Delete the recovered `crm-ncc.json`.
