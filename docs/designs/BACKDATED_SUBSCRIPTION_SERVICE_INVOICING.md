# Backdated subscription-service invoicing

## Decision

`financial.backdated_service_invoicing` owns the reviewed decision to create a
new invoice identity for consumed prepaid subscription service or reconstruct
that identity on an eligible existing invoice. It does not own invoice issue,
payment receipt, payment allocation, current access restoration, or the
subscription billing anchor.

The workflow is permission-gated, not Finance-approver-gated:

- `billing:invoice:create_backdated_service` permits new linked draft creation.
- `billing:invoice:reconstruct_backdated_service` permits documentary repair of
  one existing invoice.

Both permissions are UI-assignable and are intentionally granted to no role by
the migration. Administrators can add either permission to the roles that need
it.

## Authoritative inputs

The owner accepts an account, prepaid subscription, exact service dates, and
optionally an existing invoice. Its preview resolves and fingerprints:

- the subscription's account, plan, contract bounds and monthly cadence;
- historical price and tax terms proven by an immediately adjacent issued
  invoice for the same subscription, or by authoritative source records whose
  update timestamps prove they were unchanged from the selected period start;
- applied plan-change history; a change after the requested period start
  blocks the action because the current plan and price can no longer prove the
  historical period without a separate historical-price reconstruction;
- the canonical tax treatment and invoice-ready subtotal, tax and total;
- an exact Africa/Lagos monthly half-open service interval;
- positive RADIUS accounting intervals bound to that subscription;
- overlapping invoice periods and active service entitlements;
- any periodless, unlinked invoice whose amount and currency exactly match the
  contracted charge; and
- the account's current receivable and prepaid-funding projections.

Missing usage, invalid accounting intervals, unproven or conflicting historical
terms, contract-boundary violations, overlap, a stale fingerprint, or ambiguous
invoice evidence blocks the command. The service never infers a subscription or
period from an amount or memo. A current price or tax configuration is not
silently projected backward after its source record changed.

## Create path

The command locks the billing account and subscription, re-runs the preview,
and enters one owner transaction. It creates a draft invoice with invoice-level
period fields and one line containing:

- `subscription_id`;
- `kind = base_subscription`;
- exact period metadata;
- canonical price and tax provenance;
- a deterministic billing-line key; and
- `backdated_service_invoice = true` plus the reviewed fingerprint.

The invoice stays draft while it is being prepared. The existing invoice issue
action remains the single transition that creates the receivable and enables
normal collection. Before the command completes, the invoice owner seals a
fingerprint of the complete header, totals, active line set, tax snapshots and
review provenance. Generic header, line and edit-form actions then refuse the
draft. Issue revalidates the seal under lock and fails closed if any direct or
unexpected mutation occurred. Draft creation itself does not create a second
account charge.

The draft records its preparation instant and the effective subscriber/global
payment terms as typed due-date evidence (`contract_terms`, subscription source
reference, and `billing-payment-terms-v1`) before sealing. Issuance therefore
uses the reviewed due date without inventing or editing financial terms after
the document was sealed.

## Reconstruction path

Reconstruction accepts only an active, non-proforma draft, issued,
partially-paid, or overdue invoice on the selected account. It must have no
period, exactly one active unlinked positive line, quantity one, and exact
currency/subtotal/tax/total parity with the subscription charge. It must not
already source an active entitlement.

The invoice participant records the period, line subscription, standard base
subscription metadata, and a `reviewed_prepaid_sequence_ref` beginning with
`backdated-service-invoicing:`. Money, status, allocations and issuance dates
do not change. A paid invoice is outside this first reconstruction slice so a
historical payment cannot be reclassified without a separately reviewed money
repair.

## Payment and service consequence

Payments continue through the normal invoice-payment workflow. Whether the
customer chooses Pay Invoice, pays by transfer, or an available customer credit
is allocated automatically, the allocation points to the invoice once.

`financial.payments.resolve_payment_allocation_finalization` recognizes either
the creation marker or reconstruction reference and selects
`reviewed_document_correction`. When the invoice becomes fully paid it creates
the canonical invoice-line-backed historical `ServiceEntitlement`. It does not:

- re-anchor the period to the payment date;
- move `Subscription.next_billing_at`;
- restore present-day access; or
- create a second payment, allocation, receivable, or service debit.

If the invoice later becomes unpaid through refund, reversal, or allocation
movement, the same payment finalization path reverses its entitlement.

## UI contract

The dedicated admin page shows customer and subscription identity, action,
service dates, canonical plan/price/tax, usage evidence, overlap verdict,
matching existing invoices, current receivables, after-issue receivables, and
prepaid funds. Confirmation requires an operator reason, an explicit checkbox,
an idempotency key and the owner-produced fingerprint.

Exactly one primary action appears after a successful preview. New documents
use **Create linked draft invoice**. Existing documents use **Link invoice to
service period**. Unauthorized actions are hidden, and the service repeats the
permission and stale-state checks at command entry.

## Idempotency, audit and repair

Create and reconstruction have separate idempotency scopes. A replay returns
the original invoice; a key reused for another account fails closed. Invoice
identity, command reason, actor, usage count, period, amount and preview
fingerprint are staged in the audit event, with a durable PII-free domain event
in the same transaction.

Drift is visible as a linked line or invoice period that disagrees with the
fingerprinted audit/event evidence, an overlap introduced after preview, or a
paid marked invoice without its source-line entitlement. Re-running the same
idempotent command rebuilds only a missing command response; documentary or
money drift requires a new reviewed preview and the owning repair workflow.

## Rollout

1. Apply migration `690_backdated_invoice_permissions` and verify the database
   records the new effective core head.
2. Assign the two capabilities to selected roles. The migration deliberately
   creates no role, subscriber or system-user grants.
3. Use the controlled page for historical prepaid subscription service.
4. Keep the generic invoice form for standalone goods and services; it does
   not claim subscription service allocation.
5. Validate create, reconstruction, issue, direct payment, automatic credit
   allocation, refund/reversal, overlap and stale-preview paths before release.

The deployment and permission-assignment checks are in
`docs/runbooks/BACKDATED_SUBSCRIPTION_SERVICE_INVOICING.md`.
