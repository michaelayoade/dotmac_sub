# Customer-financial lifecycle operations runbook

Status: active

Authority: [ADR 0003](adr/0003-permanent-customer-financial-lifecycle.md)

## Operating contract

Sub owns invoice generation, overdue transitions, prepaid renewal, collections,
restoration, top-up reconciliation, subscription expiry/status commands,
customer notification delivery, and event dispatch/retry. Their scheduled owner
passes remain active. Operators may change safe cadence and local execution time;
they do not select which lifecycle phases exist.

Account outcomes are derived from canonical facts:

- `Subscriber.billing_enabled` records account approval;
- billing profile and subscription lifecycle determine eligibility;
- reviewed funding, coverage, and quarantine determine prepaid action;
- payment arrangements, proofs under review, outage shields, and grace defer
  adverse action for the affected account;
- provider capability/configuration determines whether an external transport can
  deliver its projection;
- idempotency, owner locks, and reconcilers make repeated owner passes safe.

## Daily operation

1. Review billing-health, funding, coverage, renewal, collections, notification,
   event-delivery, and access-projection observations.
2. Investigate typed blockers by account and route corrections through the
   owning payment, invoice, credit, subscription, funding, coverage, lifecycle,
   or provider service.
3. Run read-only previews before a historical repair. Apply only the reviewed,
   fingerprinted cohort through the owner command.
4. Verify money facts, service entitlement, subscription anchor, access lock,
   RADIUS projection, receipt, and customer-visible outcome after repair.
5. Keep unresolved evidence in quarantine. Do not assume zero, fabricate a paid
   period, or restore/suspend from a cache or imported identifier.

## Timing

Financial enforcement is eligible every calendar day. Configure only:

- `collections.enforcement_window_start`
- `collections.enforcement_window_end`

The window uses the configured scheduler timezone and may wrap midnight.
Notification quiet-hour start/end settings remain delivery timing policy.
Schedule cadence must enter the configured window; permanent task guards reject
disable, rename, and delete operations for customer-financial lifecycle tasks.

## Release and incident handling

Before deployment, run the repository-prescribed format, lint, architecture,
billing, payment, renewal, collections, access, notification, scheduler, and
Alembic head checks. Verify a single migration head and review any data-bearing
migration precondition.

For a faulty release, roll back the deployment or ship a focused forward fix.
For incorrect account facts, correct only the evidenced cohort through its named
owner. Do not recreate retired module, feature, settings, readiness, health,
event, or scheduler controls as incident containment.

## Required evidence

Retain non-secret references for previews, manifests, hashes, approvals,
idempotency keys, repair runs, and post-state verification. Never place customer
credentials, provider secrets, bank narration, or raw identity exports in Git,
logs, or durable knowledge.
