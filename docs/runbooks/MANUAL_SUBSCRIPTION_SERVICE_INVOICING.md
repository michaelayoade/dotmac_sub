# Manual Subscription Service Invoicing

## Scope

This runbook covers a controlled invoice for a subscription's exact next
service period. It does not cover an elapsed/backdated period or a generic
device, installation, support, or other one-off charge.

## Access setup

Assign `billing:invoice:create_subscription_service` only to the roles that
should issue forward subscription-service receivables. The migration creates an
active, UI-assignable permission with no automatic grants. No finance-approver
role is hard-coded. The web action requires a named staff principal in addition
to the permission.

## Operator procedure

1. Open the customer and locate the active subscription.
2. Select **Invoice** on that subscription.
3. Verify the account, plan, billing mode, exact period, tax, total, and stated
   settlement consequence.
4. Enter the reason for manual issuance.
5. Select customer notification only when the renewal notification should be
   requested after commit.
6. Confirm the disclosed scope and issue the invoice.
7. Verify the invoice detail shows the subscription-linked lines and exact
   billing-period header.

The operator cannot edit the service dates or amount. If the review reports an
elapsed boundary, coverage/anchor disagreement, price change, duplicate period,
conflicting invoice, or unresolved/excluded recurring component, stop and use
the named reconciliation or historical workflow. Do not create a generic
invoice as a workaround.

## Settlement behavior

- Issuance creates a receivable and does not extend access.
- A prepaid invoice that becomes fully settled creates one exact entitlement
  through the standard payment finalizer.
- Entitlement identity is bound to the invoice line; payment retries and
  alternate allocation routes do not mint a second period.
- The subscription billing anchor is not moved by invoice creation.
- Postpaid settlement closes the receivable without creating a prepaid
  entitlement.

## Rollback and deployment checks

The permission migration is additive. Its downgrade refuses to delete the
permission while any role, subscriber, or system user still holds it. Remove
those grants explicitly before downgrade.

Before deployment, rehearse the migration from the real predecessor and from a
fresh database, confirm one Alembic head, verify the permission is ungranted,
and exercise both an authorized and unauthorized customer-detail view. After
deployment, monitor audit action `issue_subscription_service_invoice`, invoice
creation failures under `financial.advance_renewal_invoicing.*`, and renewal
notification delivery when the option is selected.
