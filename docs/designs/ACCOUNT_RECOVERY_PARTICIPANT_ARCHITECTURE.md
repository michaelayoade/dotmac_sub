# Account recovery: destination architecture

## Status

Slice 1 (safe foundation) is implemented on this branch
(`fix/lifecycle-typed-intent-and-fail-closed-recovery`) as of 2026-09-14:
`customer.account_recovery` as a real owner command with genuine idempotency,
locking, a preflight fail-closed gate, and production-adapter cutover off
retired metadata/direct commits. Slice 2 (locks/add-ons/IP as registered
participants) is the next approved slice, not an open-ended "later." Thin
slices describe delivery order — they are not license to leave the
architecture undefined.

## Resource classification

| Classification | Resources | Required behaviour |
|---|---|---|
| Transactional participants | Subscriptions, add-ons, enforcement locks, IP assignments, service orders, ONT and splitter assignments, authoritative credentials/RADIUS identities | Typed snapshot/reference, locking, delete, restore/refuse, idempotency and audit evidence |
| Post-commit reconcilers | Live RADIUS/NAS sessions, served-IP projections, CPE/ONT/GenieACS configuration and readback | Consume committed events and converge external state |
| Immutable evidence | Invoices, payments, tax records, lifecycle history, audit events and recovery tombstones | Never deactivate or reactivate; finance records remain historical truth |
| Separately governed | Retention, legal hold, purge and post-purge recovery | Records-owned commands; post-purge recovery requires a versioned archive |
| Retired permanently | JSON recovery snapshots, direct boolean flips, blanket restoration and GET-triggered purge | Must never return |

## Delivery sequence

1. **Safe foundation** — Make `customer.account_recovery` a real owner
   command. Add genuine idempotency, locking and adapter cutover. Preflight
   every consequence before mutation. Initially admit only accounts where
   subscriptions are the sole affected resource.
2. **Immediate cancellation consequences** — Add participants for
   enforcement locks, active subscription add-ons, and service IP
   assignments. These come first because `cancel_subscription` already
   changes them today.
3. **Operational workflow** — Add `operations.service_order_lifecycle`.
   Preserve the exact previous state; never default a missing state to
   draft.
4. **Access identity** — Add authoritative credential and RADIUS-identity
   commands. Keep live session disconnection and device delivery
   post-commit. A missing login becomes an explicit outcome, not a failed
   restoration transaction.
5. **Physical provisioning** — Add ONT assignment. Add splitter-port
   assignment. Handle CPE assignment/configuration through its owner and a
   post-commit reconciler.
6. **Records lifecycle** — Build typed retention/legal-hold eligibility.
   Add an explicit scheduled purge command. Design post-purge recovery
   separately, only if the business actually requires it.

## Participant contract

Every participant (present and future) implements the same conceptual
protocol:

1. `plan` — identify affected records, versions and blockers.
2. `apply_deletion` — mutate only its own owned state and return evidence.
3. `restore` — compare versions and restore exactly what this deletion
   changed.
4. `verify` — report converged, deferred, conflicted, or not applicable.
5. `reconcile` — repair post-commit projections idempotently.

## Why this order

Add-ons, locks, and IP assignments are the next slice because they are
already hidden consequences of subscription cancellation — `cancel_subscription`
mutates them today even though `customer.account_recovery` cannot yet
reverse them (closed by this slice's preflight refusal, not by pretending
the consequence doesn't exist).
