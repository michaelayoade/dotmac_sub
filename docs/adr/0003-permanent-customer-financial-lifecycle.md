# ADR 0003: Permanent customer-financial lifecycle ownership

Status: accepted

Date: 2026-07-22

Decision owner: Michael / Dotmac architecture

Affected systems and domains: billing, payments, prepaid renewal, collections,
account access, subscription lifecycle, customer notifications, event delivery,
and scheduled-task administration

## Context

After the Splynx authority cutover, Sub owns the live customer-financial
lifecycle. Layered module, feature, settings, readiness, health, schedule, and
event toggles allowed different adapters to omit parts of that lifecycle. This
produced states such as confirmed payment without renewal, funding without
restoration, and active service without the corresponding owner pass.

The controls did not change the underlying business facts. They changed whether
the owning service was allowed to observe and reconcile those facts, creating
parallel operational authority and making customer outcomes depend on unrelated
configuration history.

## Decision

The canonical customer-financial owners always evaluate their eligible facts and
reconcile their consequences. Runtime module, feature, settings, scheduler, and
event enable/disable controls are not part of this authority boundary.

- Invoice generation, overdue transitions, prepaid renewal, collections,
  restoration, top-up reconciliation, subscription expiry/status commands,
  notification delivery, and event dispatch/retry remain scheduled owner work.
- Scheduled-task cadence and the shared enforcement time-of-day window remain
  configurable. Core lifecycle tasks cannot be disabled, renamed, or deleted.
- Enforcement runs every calendar day. Only
  `collections.enforcement_window_start` and
  `collections.enforcement_window_end` constrain its local execution time.
- Notification quiet-hour start/end settings remain delivery timing policy.
- Per-account approval (`Subscriber.billing_enabled`), funding, coverage,
  quarantine, invalid billing profiles, payment arrangements, payment-proof
  review, outage shields, grace, and provider/transport capability remain
  canonical facts. They determine an account outcome; they do not stop owners
  from evaluating other accounts.
- Health and drift observations remain visible but do not become hidden
  lifecycle authorities.
- Number allocation, proration, backdated-period handling, and lifecycle
  consequences follow their named owner contracts rather than runtime toggles.
- Lifecycle state, reason-scoped enforcement locks, and their durable event rows
  commit or roll back together. Delivery starts only after commit. A failed
  RADIUS, session, or financial-access consequence is reported by its handler so
  the durable event remains retryable; it is never logged as successful work.

## Authority boundary

Financial document and event owners write money facts. Resolvers derive funding,
coverage, billing profile, shields, and access decisions. Lifecycle/access owners
apply suspend or restore consequences. Scheduler and delivery adapters invoke
owners and transport outcomes; they do not decide whether the domain exists.

## Migration and cutover

Migration 398 deletes the retired settings instead of preserving tombstones and
re-enables the permanent scheduled-task rows. The registry, settings UI, module
manager, task adapters, notification handlers, and enforcement planner no longer
read those settings. Historical readiness rows remain evidence only and do not
gate runtime decisions.

This is a forward-only authority cutover. A downgrade does not recreate the
retired controls. Any future need to change financial behavior must be expressed
as a business fact or owner policy with an explicit contract and tests, not as a
generic lifecycle bypass.

## Consequences

Deployments cannot leave one lifecycle phase running while another is disabled.
Account-specific ambiguity remains fail-safe through quarantine, shields, and
invalid-profile outcomes. Operators repair incorrect facts through their named
owners while unaffected accounts continue through the canonical lifecycle.

Operational containment of a faulty release is deployment rollback or a focused
forward fix. Provider outages remain bounded by provider capability and retry/
idempotency contracts; they do not change money ownership.

## Verification

- Architecture tests assert retired financial controls are absent from the
  registry and settings specification.
- Behavior tests prove stale disabled rows cannot suppress owner work.
- Scheduler tests reject disable, rename, and delete operations for permanent
  lifecycle tasks.
- Enforcement tests prove weekends and holidays are ordinary days and only the
  shared time-of-day window defers consequences.
- Lifecycle tests prove rollback emits no external consequence, and enforcement
  handler tests prove incomplete projections remain durable retry work.
- Account quarantine, shields, grace, funding, coverage, payment, renewal,
  suspension, restoration, notification, and idempotency suites remain green.
- Alembic has one head and migration 398 removes the retired rows while enabling
  the canonical tasks.

## Rollback or forward-fix

Forward-fix the owning service or roll back the release. Reintroducing a retired
runtime control is not an accepted rollback because it restores split authority.

## Review and retirement

Review after the first full billing cycle in production and after any material
change to billing or access ownership. Supersede this ADR only with an explicit
replacement authority contract that prevents parallel decision paths.
