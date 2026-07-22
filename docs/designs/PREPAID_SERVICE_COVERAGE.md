# Prepaid Service Coverage and Enforcement

Status: cut over in code; production enablement remains readiness-gated.

## Ownership

`financial.prepaid_service_coverage` is the sole resolver for current prepaid
service coverage. It consumes facts; it never advances service dates, writes
money, restores access, or suspends service.

Positive current-coverage evidence, in priority order, is:

1. An active `ServiceEntitlement` spanning the decision time. It is linked to
   either the exact customer-funded ledger entry or an append-only
   `SubscriptionBillingGrant`; new service periods must use one of these exact
   sources.
2. An applied `ServiceExtensionEntry` spanning the exact added interval
   `[previous_next_billing_at, new_next_billing_at)`. This is a non-financial
   service grant and does not fabricate a funded entitlement.

A paid invoice is financial source evidence, not read-time access evidence. The
coverage reconciler must project its exact subscription line and ordered billing
period into `ServiceEntitlement` before it covers service.

`Subscription.next_billing_at` is a projection. A future value without one of
those evidence rows is `unresolved_projection`: it blocks adverse enforcement
and enters reconciliation, but it does not authorize restoration.

## Access policy

- Current canonical coverage wins over account reserve balance. A customer is
  not suspended during an already funded or explicitly granted service period
  merely because the configured minimum balance is not held for the next one.
- `min_balance` remains a top-up/reserve target. It becomes an access threshold
  only when at least one collectible service is actually due and uncovered.
- A prepaid consequence targets only due, uncovered subscription IDs. It never
  applies an account-wide lock to covered or unresolved services.
- Restoration releases a prepaid lock only when the account can fund all due
  service, or the exact locked subscription has current canonical coverage.
  Other enforcement reasons remain untouched.
- A future billing anchor without evidence is fail-safe quarantine. It is not
  sufficient to bulk restore or to suspend.

## Renewal and control-plane cutover

`financial.prepaid_service_renewals` is the only writer of newly funded prepaid
periods. Its transaction writes the debit, entitlement, paid-through projection,
and durable outcome together.

The competing `billing.prepaid_monthly_invoicing` control, its legacy
`prepaid_monthly_invoicing_enabled` alias, and the scheduled draft-invoice path
are retired by migration 392. Downgrade does not recreate that owner.

`billing.prepaid_service_renewals` remains the production writer control. While
it is off,
`collections.prepaid_balance_enforcement` is continuously blocked from warnings
and suspension. Every enforcement OFF-to-ON transition requires a new,
unactivated readiness record; a previously activated record cannot be reused.

The standalone `collections.prepaid_enforcement_activation_at` setting is
retired by migration 393. The signed readiness record owns the intended
activation time; the feature control owns enabled/disabled state. This removes
a second activation clock without removing the emergency kill switch.

The former `PaymentPrepaidApplication` runtime is also retired. Its rows are
historical payment-funded-period, ledger, entitlement, and access-recheck
provenance; they are not current coverage or renewal authority. Migration 394
atomically renames the physical table to
`payment_prepaid_applications_archive`, preserving every row, constraint, and
index while leaving it without an application model or writer. Finance
operations owns archive retention. Migration 396 creates the same empty archive
shape only for an environment that had already applied the original
empty-table-only form of migration 394. Both revisions validate the complete
column, type, nullability, default, primary-key, foreign-key, check-constraint,
index, and row-count contract. Migration 394 rejects neither-table and
both-table states and accepts archive-only state only after that validation.
Migration 397 applies the same fail-closed validation to databases already at
396. Alembic autogeneration excludes the verified archive from contract
proposals. The archive is append-only operational evidence and requires a
separate reviewed retention decision before deletion.

The enforcement kill switch remains. Coverage integrity, canonical-renewal
availability, currency validity, and fresh enablement readiness are not optional
health flags and cannot be bypassed by disabling generic health checks.

## Coverage reconciliation

`financial.prepaid_service_coverage_reconciliation` owns historical coverage
repair. Its read-only preview classifies the complete or selected cohort as:

- already covered by one entitlement or one exact extension interval;
- repairable from exactly one active, fully paid invoice line or one unreversed
  `prepaid_service_renewal` adjustment with a linked active debit and structured
  period;
- legitimately due and uncovered, requiring no historical repair; or
- quarantined because evidence is absent, malformed, duplicated,
  contradictory, or belongs to an inactive parent account.

Confirmation requires the exact preview `as_of`, SHA-256 fingerprint,
idempotency key, operator identity, and reviewed reason. It locks accounts,
subscriptions, and financial source rows in deterministic order, recomputes the
preview, creates only the exact missing entitlement, records immutable run/item
evidence, and stages `prepaid_coverage.reconciled` in the same transaction. It
never posts money, edits a balance, or infers a period from memo text.

The operator adapter is:

```bash
python scripts/billing/prepaid_coverage_reconcile.py --as-of <ISO-8601>
python scripts/billing/prepaid_coverage_reconcile.py \
  --apply --as-of <same-ISO-8601> --fingerprint <sha256> \
  --idempotency-key <stable-key> --actor <operator> \
  --reason "<reviewed evidence reason>"
```

`--subscription-id` may bound investigation and staged repair. Enforcement
readiness always evaluates the full cohort.

## Continuous acceptance gate

Operations must continuously report:

- future `next_billing_at` with no current coverage evidence;
- active prepaid locks on currently covered subscriptions;
- overlapping or duplicate current entitlements;
- renewal debit without exactly one matching entitlement;
- entitlement/anchor mismatch;
- due uncovered service while canonical renewals are disabled.
- exact invoice/renewal evidence still requiring entitlement projection;
- quarantined coverage evidence by stable reason code.

The paid-invoice fallback is removed. Any repairable or quarantined item blocks
warnings and suspension globally on every sweep, including after initial
activation. Positive restoration may still proceed from exact coverage, so the
gate cannot strand a repaired customer. Readiness records the zero-gap evidence
hash and cannot be recorded while a repairable or quarantined item remains.

## Production cutover runbook

1. With enforcement disabled, inventory both
   `payment_prepaid_applications` and `payment_prepaid_applications_archive`.
   Both names existing at once is an ambiguity and blocks deployment. Deploy
   migrations 392 through the current head; migration 394 must leave exactly
   one archive table with the same row count as the legacy source, and migration
   397 must validate its complete schema before any reconciliation apply.
2. Enable and dry-run `billing.prepaid_service_renewals`; repair missing prices,
   baseline quarantine, malformed service periods, and parent/subscription
   lifecycle drift.
3. Run the full coverage preview. Apply only exact repairable items. Resolve
   quarantine through the named financial, invoice, extension, or lifecycle
   owner; never edit an entitlement or lock with SQL.
4. Repeat until `repairable_count=0`, `quarantined_count=0`, and the health
   observations `prepaid_coverage_repair_required` and
   `prepaid_coverage_quarantined` are zero.
5. Preview the exact prepaid-lock cleanup cohort with
   `python -m scripts.one_off.unwall_paid_accounts --prepaid-locks-only`.
   Apply a staged sample with `--prepaid-locks-only --apply --limit 1`, then the
   reviewed cohort with `--prepaid-locks-only --apply`. The selector
   begins from active prepaid locks and consumes the financial-access owner's
   exact restoration preview; it does not use subscriber status, a paid invoice,
   or `next_billing_at`. Verify Sub access state and RADIUS projection.
6. Record a fresh full-cohort readiness generation with its intended activation
   time, then deliberately enable `collections.prepaid_balance_enforcement`.
7. Verify the first sweep and the next scheduled renewal/sweep cycle before
   closing the cutover. Verify the payment-application archive remains present
   with its pre-deploy row count. Retain the kill switch; do not recreate retired
   flags or a payment-application runtime writer.

## Transaction boundaries

Coverage resolution is read-only. Reconciliation is an owner-managed command.
Renewal confirmation no longer imports HTTP types or completes a transaction;
its caller's registered owner/coordinator supplies the transaction boundary.
Extension and financial access owners lock and write their own state and emit
their own durable evidence.
Routes, jobs, webhooks, and provider adapters request these decisions; they do
not reproduce coverage rules or translate domain state into HTTP exceptions
inside the owner.
