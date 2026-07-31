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

## Ownership

`financial.advance_renewal_invoicing` owns cohort eligibility, exact future
period selection, idempotent invoice construction, and the transactional
`subscription.renewal_invoice_ready` request. It consumes typed recurring-charge
previews from the existing prepaid or postpaid owner and delegates invoice and
line persistence to `financial.invoices`.

The scheduled task is an adapter. It reads the cohort, then invokes one owner
command per subscription on a transaction-free session.

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

## Configuration

- `billing.renewal_invoice_notice_enabled`: false unless explicitly enabled.
- `billing.renewal_invoice_notice_days`: nullable integer from 0 through 90.

Enabling without a day is rejected by the admin settings owner. Runtime also
fails closed if persisted configuration is missing or malformed.

## Idempotency and delivery

Subscription UUID, exact start/end, and component form the unique billing-line
identity. A matching retry returns the existing invoice and does not emit a
second notification. Invoice, lines, audit evidence, and the domain event are
staged in one owner transaction.

The customer notification contains the exact portal invoice URL and uses the
canonical invoice PDF attachment resolver. Attachment failure retries the whole
email; body-only fallback is forbidden.

## Operations

The daily scheduler remains harmless while disabled. Enable only after setting
and reviewing the notice day. Monitor created, replayed, and failed counts; a
coverage/anchor disagreement or conflicting future invoice requires review and
is never repaired automatically.
