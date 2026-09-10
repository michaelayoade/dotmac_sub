# Expense claim ERP cutover

## Authority and release point

- Sub owns the technician's expense request and the Field manager's approval or
  rejection decision.
- ERP owns the accounting claim, payment intent, transfer execution,
  reconciliation, and final paid fact.
- Submission is the first release point. It creates an ordered ERP delivery
  intent so the ERP claim is visible as `SUBMITTED` before manager approval.
- Approval, rejection, and payment initiation each commit atomically with their
  own outbox row. ERP delivery and status reconciliation remain asynchronous and
  idempotent.

## Deployment prerequisites

1. Apply the existing `field_erp_sync_events` and `sync_flow_ownership` migration
   chain through repository head.
2. Enable the typed ERP outbox-delivery and expense-status capabilities.
3. Verify the ERP service identity has `sub:expense:write` for claim creation,
   status, and Field manager decisions. Grant the separate exact
   `sub:expense:pay` scope only to the Sub integration identity that may request
   a transfer; do not grant broader human or finance-administration permissions.
4. Confirm the ERP accepts `source_claim_id` and the stable
   `exp-{request_id}-submit-v1` idempotency key.
5. Confirm the previous expense sender is disabled before changing ownership.

## Controlled activation

1. Record and retain the pre-cutover legacy owner for
   `sync_flow_ownership.expense_claim` while deploying and validating the
   application change.
2. Before ownership cutover, verify a submitted expense creates no outbox row.
3. Assign `expense_claim` ownership to `sub` through the reviewed production
   configuration procedure.
4. Submit one newly created canary expense and verify its submit event reaches
   `accepted`, the ERP claim is exactly `SUBMITTED`, and the ERP claim reference
   is projected back to Sub.
5. Approve the canary in the Field app and verify the ordered approval event is
   accepted and the ERP claim becomes `APPROVED`, not `PENDING_APPROVAL`.
6. With a dedicated payment-authorized manager, select **Pay expense** and verify
   one payment event is staged. Confirm ERP creates one payment intent and reports
   `PROCESSING` (or `COMPLETED` for an immediate success).
7. Exercise the webhook or polling path and verify `COMPLETED` changes the ERP
   claim and both Field views to `PAID`. Exercise an indeterminate sandbox result
   and verify no automatic duplicate transfer is attempted.

## No historical backfill

Do not enqueue or replay previously submitted or approved expenses during this
cutover. Existing records are intentionally excluded. This branch
contains no migration, startup hook, scheduled scan, or repair command that
backfills them. Any future historical repair requires a separate reviewed scope.

This prohibition concerns ERP delivery. Revision
`587_field_request_requester_history` separately repairs exact local requester
identity links so staff can see claims they raised; it does not approve, enqueue,
or replay an expense claim. Before rollout, record the count of active expenses
with a null `requested_by_system_user_id`. After migration, investigate every
remaining row rather than inferring an owner. Verify a repaired claim appears
for its requester through `GET /api/v1/field/expense-requests` and that it
remains invisible to another requester.

## Rollback

Restore the recorded pre-cutover legacy ownership of `expense_claim` to stop new
Sub deliveries, then disable the expense delivery/status capabilities. Do not
delete outbox evidence.
Investigate any pending event by its event ID before deciding whether to resume
delivery. Production configuration and ownership changes are operational work and
are not performed by this branch.
