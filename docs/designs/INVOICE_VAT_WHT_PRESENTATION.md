# Invoice VAT and Withholding-Tax Presentation

## Decision

`financial.invoices` owns the tax presentation facts printed on an issued
customer invoice. Active invoice-line tax snapshots supply the VAT percentage.
At draft-to-issued transition, the invoice owner reads the authoritative
customer WHT policy and billing WHT percentage once and stores the policy
version, rate, deduction, and net amount payable on the invoice.

The legal invoice total remains the VAT-inclusive gross value. WHT is a payment
deduction and receivable, not a discount or reduction of invoice revenue:

```text
withholding tax = VAT-exclusive subtotal × snapshotted WHT percentage / 100
net amount payable = gross invoice total − withholding tax
```

Money is rounded to two decimal places using the shared half-up money rule.
Customers without WHT enabled retain NULL WHT snapshot fields and owe the gross
invoice total. Existing invoices are not reclassified or backfilled because a
current checkbox cannot prove the policy applicable at their issue time.

## Presentation

Customer invoice HTML, canonical PDF, text-PDF fallback, and image-PDF fallback
render `VAT (<rate>%)`. Mixed-rate invoices list every active positive rate.
When an invoice has a WHT snapshot they also render `WHT (<rate>%)` as a
deduction and `Net amount payable`. The ordinary `Total` and `balance_due`
remain gross accounting facts.

Invoice email attachments use the same canonical PDF export as portal and
administrative downloads. Cache freshness follows `Invoice.updated_at`, so the
issuance snapshot invalidates any earlier draft export.

## Payment consistency

Invoice-linked direct bank-transfer intents consume the invoice's immutable WHT
snapshot. They no longer recalculate from the current customer policy or current
global rate. Arbitrary deposits and card/online checkout do not apply WHT.

## Migration and verification

Migration `702_invoice_withholding_tax_snapshot` adds nullable snapshot columns
and a database check requiring complete, positive, arithmetically consistent
WHT evidence. The migration is additive and performs no historical backfill.
Downgrade removes the check and columns.

Focused owner, transfer-intent, customer-invoice, PDF, migration, and
architecture tests enforce the snapshot and presentation contract.
