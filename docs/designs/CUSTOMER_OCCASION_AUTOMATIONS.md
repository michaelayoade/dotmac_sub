# Customer birthday and service anniversary emails

Status: implemented for review; both automations ship disabled.

## Authority and behavior

`communications.customer_occasions` owns typed configuration, calendar matching,
bounded admission, review, and delivery preflight. The dedicated
`customer_occasion_automations` table contains one versioned configuration for
each occasion. Migration 691 seeds both disabled; missing configuration fails
closed. The scheduler never enables a greeting. Configuration is an admission
control, not a transport control: pausing stops new admissions while already
accepted mail continues through delivery eligibility checks.

Subscriber date of birth is the birthday input. Subscriber `account_start_date`
is the original service anniversary input. There is no fallback to record
creation, import time, renewal, current subscription start, or first payment.
Operators must verify imported account start dates before enabling anniversary
messages. Missing dates are excluded and counted on the review page. This
change does not invent or backfill joining dates or claim that all existing
dates have been verified.

Anniversary admission is restricted to exactly `active` and `suspended` account
statuses, starting after one complete year. `notification_status_policy` owns
the `anniversary` category status rule; it intentionally permits suspended
customers for this user-authorized greeting. Birthday status eligibility uses
the existing policy (terminal and walled accounts do not get nonessential mail).
Both categories are marketing for suppression purposes, require subscriber
marketing opt-in, and consume the general notification preference. The primary
customer email is the recipient; contacts and resellers do not receive personal
greetings. Blank primary email addresses are excluded from admission.

The five-minute permanent task delegates bounded processing to the owner. Each
occasion checks the current local date after its configured send time (09:00
Africa/Lagos initially). Only today's matches are admitted; no historical
catch-up occurs. February 29 is observed on February 28 in non-leap years and
February 29 in leap years. Templates are plain text with `{first_name}` and
`{years}` placeholders; output is HTML-escaped before email delivery. SMTP and
sender identity remain owned by existing system email configuration.

## Atomicity, idempotency, audit, and repair

Every public write enters `execute_owner_command` once on a transaction-free
session. Saves lock the configuration and reject stale revision submissions.
Batch admission locks the configuration (skipping a busy worker) then customer
UUIDs in stable order. The config lock serializes each occasion. Intent dedupe
keys name occasion, customer UUID, and local calendar year; the existing unique
intent index is the durable annual winner. Suppressed intents count as consumed
annual decisions and are not automatically revived when consent changes.
Admission is bounded to 200 customers per occasion per tick; later ticks drain
remaining same-day matches. A failed transaction retries as a whole.

The existing intent planner owns preference, suppression, and recipient
decisions and stages its evidence in the same transaction. Notifications remain
the only delivery outbox. Queue delivery consults a typed occasion preflight
that rechecks live status, opt-in, preferences, and unchanged primary address;
the existing suppression ledger is checked immediately before transport.
Provider retries and outcome projection stay in the notification owner. No
direct provider call or parallel birthday/anniversary sender exists.

Configuration updates and completed admissions stage version-1 record-only
`customer_occasion.changed` audit evidence with revision and aggregate counts.
Communication intents retain source, calendar year, and configuration revision;
notification history retains provider outcomes. Read freshness is request-time.
The owner rebuilds review information from configuration, subscriber facts, and
intents without replaying delivery. Missing-date counts and suppressed/failed
outcomes are drift signals. Repair dates through existing customer-profile
owners and repair delivery through the existing notification retry path; never
delete annual keys to force another greeting.

## Review page contract

Screen: `/admin/notifications/occasions`, linked from Notification Setup.
Audience: notification administrators reviewing and operating annual greetings.
Read permission: `notification:occasion:read`; save permission:
`notification:occasion:update`; explicit test-email permission:
`notification:occasion:test`. The migration grants these to the admin role;
other roles can receive each independently. Authentication and permissions are enforced server-side.
The page consumes the typed `OccasionReviewResult` from the owner.

First viewport shows purpose, disabled/enabled state, matching and missing-date
counts, anniversary status restriction, and editable content. Each occasion has
one save action. Controls include timezone, hour/minute, and independent enabled
checkboxes. No configuration is automatically enabled by save or test actions.
Read-only users see disabled fields and no write actions. Example rendering and
up to ten matching customers are progressively disclosed. Customer names and
emails require the same notification read authorization as the existing queue;
no birth years or raw birth dates are displayed. Preview eligibility is
request-current and does not reserve an annual send. Empty matches, disabled
state, missing dates, stale-save errors, and authorization failure remain
distinct. Recent source activity is capped at 20 rows; the existing notification
history is the provider-outcome drill-down. Tables scroll horizontally on mobile;
form controls wrap. The page provides a sample email; operators may explicitly
queue a `[TEST]` copy to an entered email using saved content without enabling
customer sending. Tests are administrative operational messages with separate
command-ID deduplication and do not consume a customer annual greeting.

## Rollout and validation

Apply the additive migration through the normal release process. Verify both
rows are disabled, audit customer dates, edit and preview templates, explicitly
send test copies if desired, and review opt-in coverage. Enable each occasion
only after review. There is no production or deployment action in this change.

Fast unit tests are non-authoritative SQLite/model-metadata evidence. PostgreSQL
acceptance must use `make test-integration` with an explicit disposable
`TEST_DATABASE_URL`, migrated through the real chain. Fresh-baseline and real
690-to-691 migration rehearsals are required before release. Migration 691 is
additive and refuses destructive downgrade; retirement requires a forward fix.


## Starter extraction contract

Michael approved the narrowly scoped Sub-first exception for PR #3658 on
2026-10-16 after the feature freeze was explained: "Yes, keep them in Selfcare."
The exception and the additive configuration migration are recorded in
`docs/feature_freeze_exceptions.json`; CODEOWNERS review remains required.
Both automations remain disabled for review. No other capability is exempted.

The extraction target is **Starter Communications annual-greetings/v1**, a
proposed contract rather than an existing published package. It comprises:

- Immutable occasion configuration: occasion enum, enabled admission state,
  schedule/timezone, plain-text content, and optimistic revision.
- Immutable recipient facts supplied by Sub: customer UUID, authoritative
  birth/original-start date, account status, primary email, consent and
  preference evidence, with source and observation time.
- Typed local-calendar evaluation and outcomes: eligible/suppressed reason,
  completed years, and the stable occasion/customer/calendar-year identity.
- Typed requests to the incumbent communication-intent participant; provider
  credentials, direct sending, billing decisions and guessed joining dates
  are outside this contract.

A future published, pinned package consumes these facts through a Sub adapter.
Cutover requires preservation of configuration revisions and annual intent keys,
parity of status/consent/date behavior, and removal of the Sub implementation in
the same ownership slice. Sub remains authoritative until that separate,
reviewed migration; there is no shadow writer or automatic future enablement.
