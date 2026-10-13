# Backdated subscription-service invoicing rollout

Owner: `financial.backdated_service_invoicing`

This runbook enables the permission-gated UI for creating or reconstructing an
invoice for an already-consumed prepaid subscription period. It does not grant
either capability automatically and does not authorize a production deployment
by itself.

## Preconditions

- Select the exact `origin/main` commit through the normal release process and
  deploy its immutable candidate digest to staging first.
- Confirm the database migration chain currently resolves to
  `686_field_lifecycle_router_health_merge` before applying this slice.
- Confirm a rollback window and database backup evidence under the standard
  migration runbook.

## Migration

Apply the normal Alembic chain. Migration
`690_backdated_invoice_permissions` extends the effective core head and creates
these active, UI-assignable permissions:

- `billing:invoice:create_backdated_service`
- `billing:invoice:reconstruct_backdated_service`

The migration creates no rows in `role_permissions`,
`subscriber_permissions`, or `system_user_permissions`. Downgrade refuses while
any direct or role grant remains, so operators must remove those grants before a
rollback can delete the permission records.

## Permission assignment

After migration, use the existing Roles UI to add only the required capability
to each approved operational role. Creation and reconstruction are independent;
a role may receive either or both. Record the role change in the deployment
evidence and verify a user outside those roles sees neither action and receives
403 from direct requests.

## Staging acceptance

Use a non-production customer fixture with an exact prepaid subscription and
subscription-bound RADIUS usage. Verify:

1. an adjacent issued invoice or unchanged authoritative source records prove
   the selected historical price and tax terms;
2. create produces one linked, sealed draft without changing current access or
   `Subscription.next_billing_at`;
3. the generic invoice editor refuses the sealed draft;
4. issue succeeds only while the owner seal matches the complete document;
5. one normal payment allocation settles the invoice and creates one historical
   entitlement for the selected period;
6. refund, payment reversal, or allocation reversal makes the invoice unpaid
   and revokes that entitlement; and
7. reconstruction preserves the selected invoice's money, status, allocations
   and issue date.

Capture invoice, line, allocation and entitlement identifiers without customer
PII. Production authorization still requires the repository's normal staging
acceptance document for the exact candidate digest.

## Failure and rollback

- A stale preview, conflicting adjacent terms, missing usage, overlapping
  coverage, or seal mismatch is a safe refusal. Do not bypass it with the
  generic invoice editor.
- Before issue, void the controlled draft and produce a new preview when the
  document must change.
- After issue, use the named invoice, payment, refund, reversal and credit-note
  workflows; never edit issued evidence.
- To roll back the permission migration, remove every direct and role grant,
  verify all three grant tables have zero rows for both keys, then run the
  migration downgrade. Existing invoice evidence is retained.
