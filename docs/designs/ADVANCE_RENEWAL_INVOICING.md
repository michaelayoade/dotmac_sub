# Advance Renewal Invoicing

Status: implemented

## Purpose

Billing operations may explicitly enable advance renewal invoices and choose an
exact number of calendar days before current service coverage ends. There is no
default notice day. Disabled, absent, or invalid configuration produces no
invoice and no customer notification.

This capability is separate from terminal `subscription.expiring` reminders.
Those continue to describe `Subscription.end_at`; advance renewal invoicing
describes the next billable service period anchored to current coverage.

The same owner also exposes a reviewed manual action for the exact next service
period. This is the forward-looking counterpart to historical/backdated invoice
repair: operators do not type dates or amounts. They select the subscription,
review the owner-derived period and price, and explicitly confirm an issued
receivable.

## Ownership

`financial.advance_renewal_invoicing` owns per-subscription eligibility, exact future
period selection, idempotent invoice construction, and the transactional
`subscription.renewal_invoice_ready` request. It consumes typed recurring-charge
previews from the existing prepaid or postpaid owner and delegates invoice and
line persistence to `financial.invoices`.

Subscription lifecycle and payment events invoke one timer-scheduling owner
command for the exact subscription. The shared durable-timer runtime emits the
declared trigger when due; the billing lifecycle adapter then invokes the
invoice owner on a transaction-free session.

The admin manual adapter requires the independently assignable
`billing:invoice:create_subscription_service` permission. The permission is
created without default role grants. It is not a finance-approver role check;
operators grant the capability to whichever staff roles need it through RBAC.
Execution still requires a named staff principal so the audit evidence cannot be
anonymous.

## Date contract

For a monthly service covered through September 1 and configured at seven days:

```text
evaluated/issued_at: August 25
due_at:              September 1
billing period:      September 1 through October 1
```

Invoice generation never writes `Subscription.next_billing_at`. For prepaid
service, active entitlement or extension evidence must end exactly at the
projected anchor. Confirmed payment later creates the exact future entitlement;
the established coverage owner then projects the new anchor.

The manual action accepts only a period whose start is still in the future. An
elapsed period is rejected and belongs to the separate reviewed historical
workflow. This separation prevents a current renewal command from silently
being used as a backdating or repair tool.

## Reviewed manual workflow

The customer subscription row links to a dedicated review page when the caller
holds the manual permission. The page shows the customer account, plan, billing
mode, exact start/end, canonical recurring charge, tax, total receivable, and
settlement consequence. It carries an owner-produced fingerprint, an explicit
operator reason, an idempotency key, and required confirmation.

On confirmation the owner locks the subscription and rechecks account identity,
active lifecycle state, future billing anchor, current prepaid coverage evidence,
terminal date, recurring price/tax, duplicate billing-line identity, conflicting
account-period invoices, unresolved or excluded recurring components, and the
preview fingerprint. A mismatch fails closed and returns the operator to a fresh
review.

The result is an issued invoice, not an editable draft. Its header and every line
carry the exact period, every line carries the subscription UUID, and the shared
billing-line key is identical to scheduled advance renewal invoicing. The legacy
admin `generate-from-subscription` POST writer is removed. Generic invoice
authoring remains available for non-subscription charges but does not become a
parallel service-period writer.

For prepaid service, invoice creation never grants access. Full settlement uses
the existing payment finalizer to create one entitlement keyed by the invoice
line, so a retry or a different payment allocation path cannot create a second
service allocation. For postpaid service, settlement closes the receivable and
the period remains documentary; it does not create a prepaid entitlement.

## Configuration

- `billing.renewal_invoice_notice_enabled`: false unless explicitly enabled.
- `billing.renewal_invoice_notice_days`: nullable integer from 0 through 90.

Enabling without a day is rejected by the admin settings owner. Runtime also
fails closed if persisted configuration is missing or malformed.

Manual issuance does not depend on notice-day configuration; it depends on its
dedicated RBAC permission and exact reviewed preview.

## Idempotency and delivery

Subscription UUID, exact start/end, and component form the unique billing-line
identity. A matching retry returns the existing invoice and does not emit a
second notification. Invoice, lines, audit evidence, and the domain event are
staged in one owner transaction.

Manual issuance emits the renewal notification only when the operator selects
the disclosed notification option. Whether or not notification is requested,
the audit row records actor, reason, command/correlation identity, fingerprint,
period, total, billing mode, and settlement consequence atomically with the
invoice.

The customer notification contains the exact portal invoice URL and uses the
canonical invoice PDF attachment resolver. Attachment failure retries the whole
email; body-only fallback is forbidden.

## Operations

No business-wide financial sweep is installed. Enable only after setting and
reviewing the notice day. Subsequent subscription lifecycle and payment events
create or replace the exact subscription timer. A coverage/anchor disagreement
or conflicting future invoice requires review and is never repaired automatically.

Before enabling staff access, assign the dedicated permission to the intended
roles and verify that the customer-detail action remains absent for unassigned
roles. See `docs/runbooks/MANUAL_SUBSCRIPTION_SERVICE_INVOICING.md`.
