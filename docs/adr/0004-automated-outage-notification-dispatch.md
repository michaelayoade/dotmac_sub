# ADR 0004: Automated outage notification dispatch

Status: proposed

Date: 2026-07-23

Decision owner: Michael Ayoade

Affected systems and domains: outage classifier (`app/services/topology/`), customer notifications, support inbox load

## Context

`app/services/topology/outage_notifications.py` was built with a deliberate
safety property, stated in its own module docstring:

> real dispatch requires ALL of — `OUTAGE_NOTIFY_ENABLED` on, an explicit
> operator `actor_id` (there is NO Celery beat / auto-trigger that sends to real
> customers), the boundary passing the confidence gate, the boundary not
> debounced, and the per-run cap.

The reasoning was sound: sending to every customer behind a boundary is
irreversible and high-blast-radius, so a human should look at the plan first.

Production shows the property did not survive contact with operations. Between
2026-07-06 and 2026-07-23 the classifier recorded **3,723 incidents** — 612
resolved `node_outage` incidents summing 18,228 subscriber-affections — and
`outage_notification_dispatches` contains **zero rows**. Nobody has ever clicked
the button. The preview page works; it is simply not part of anyone's job.

Meanwhile the CRM inbox absorbed the gap: over 180 days, 6,544 conversations
mention an outage, 1,877 are pure "any update?" chasing and 1,906 ask when an
engineer will arrive. Customers are the outage-detection channel of last resort.

A gate that is never passed is not a safety control. It is an outage
notification system that does not notify.

## Decision

Add an automated trigger, owned by `app.services.topology.outage_auto_notify`,
which selects automation-eligible incidents and calls the existing
`dispatch_outage_notifications`.

- **Authoritative record** — unchanged. `OutageIncident` remains the incident
  record; `OutageNotificationDispatch` remains the audit and debounce source.
- **Canonical writer of a dispatch** — unchanged.
  `outage_notifications.dispatch_outage_notifications` stays the only path that
  emits a customer outage notification. Automation supplies a trigger and an
  actor; it does not gain a second send path.
- **Channel selection** — unchanged, and still not the outage notifier's.
  `notification_channel_policy` resolves channels (ADR context:
  `docs/designs/NOTIFICATION_CHANNEL_POLICY.md`).
- **Who is affected** — moved. `incident_subscription_ids` moves out of the
  admin route into `app.services.topology.outage_targets`, because a scheduler
  cannot import a route helper and two copies would drift.

Automation is deliberately **narrower** than a manual dispatch. An operator can
look at a marginal incident and judge it worth sending; the scheduler cannot, so
it only acts where judgement is not required:

| Guard | Manual | Automated |
|---|---|---|
| `OUTAGE_NOTIFY_ENABLED` | required | required |
| `OUTAGE_AUTO_NOTIFY_ENABLED` | n/a | required (default off) |
| Dry-run default | n/a | **on** |
| Classification | any customer-visible classifier incident | `node_outage` only |
| Settling period | none (human judges) | 15 min customer-visible |
| Min affected | area gate only | additional explicit minimum |
| Per-run cap | recipients | recipients **and** incidents |
| Actor | operator UUID | `AUTO_ACTOR_ID` sentinel |

`radio_cluster` is excluded because it is not trustworthy enough to automate:
2,252 of 2,459 production `radio_cluster` incidents ended `discarded`.

## Invariants

- No second send path. Every customer outage notification goes through
  `dispatch_outage_notifications` and writes an `OutageNotificationDispatch` row.
- The existing confidence gate, persisted debounce, opt-out check and per-run
  recipient cap apply identically to automated and manual dispatch.
- Automated and manual sends are always distinguishable in the audit, via
  `actor_id == AUTO_ACTOR_ID`.
- Automation requires two independent flags. Enabling operator dispatch never
  silently enables the scheduler.
- A fault that clears inside the settling window never reaches a customer.
- The outage notifier never names a channel.
- Concurrent runs cannot double-notify: single-flight advisory lock.

## Consequences

**Operational.** Customers hear about node outages without a human in the loop.
Blast radius is bounded by the per-run incident cap × per-run recipient cap and
by the debounce window. The failure mode that matters is notifying about an
outage that is not real; the settling window, `node_outage`-only restriction and
high-confidence gate are the mitigations, and dry-run is the way to measure it
before committing.

**Support load.** This is the point. The 6,544 outage conversations and 1,877
status chases are the cost being targeted.

**Reversibility.** Setting `OUTAGE_AUTO_NOTIFY_ENABLED=false` returns the system
to operator-only dispatch with no code change and no data migration.

**Rejected — leave it manual.** Tried for 17 days across 3,723 incidents; zero
dispatches. Rejected on evidence.

**Rejected — notify on `suspected`.** Reaches customers faster, but the
classifier's own lifecycle treats `suspected` as unconfirmed, and a false
"your area is down" is worse than silence.

**Rejected — automate `radio_cluster` too.** 92% of them are discarded.

## Migration and cutover

- **Old owner and paths:** operator-only, `POST /admin/network/detected-outages/notify`. Retained, unchanged.
- **New owner and paths:** `app.services.topology.outage_auto_notify.auto_dispatch_due_outage_notifications`, scheduled by `app.tasks.outage_auto_notify`.
- **Backfill/repair:** none. Historical incidents are not retro-notified — a notification about a resolved outage is noise.
- **Shadow/verification phase:** `OUTAGE_AUTO_NOTIFY_DRY_RUN=true` (the default) plans and logs recipients without sending. Run it for several days and read `outage_auto_notify_dry_run` log lines against the incidents the NOC saw.
- **Cutover gate:** dry-run output shows no incident that the NOC would not have notified about manually, and per-run recipient counts are within expectation.
- **Fallback retirement:** none. The manual path stays as the way to notify about incidents automation deliberately excludes (`radio_cluster`, operator-declared, below-threshold).
- **Schema contract step:** none. No migration; `actor_id` already carries no FK.

## Verification

- `tests/test_outage_auto_notify.py` — eligibility gates, dry-run, actor
  stamping, disabled no-op, and that automation adds no second send path.
- `tests/architecture` — adapter transaction ownership and decision-input
  baselines, on seabone.
- Production verification after enabling: `outage_notification_dispatches`
  accumulates rows with `actor_id = AUTO_ACTOR_ID`, and CRM outage-conversation
  volume is the outcome measure.
