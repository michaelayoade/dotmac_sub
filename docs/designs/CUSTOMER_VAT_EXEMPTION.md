# Customer VAT Exemption

## Decision

VAT exemption is an explicit, administrator-managed customer policy. It is
displayed as a checkbox beside the withholding-tax setting on person and
business customer forms. New and existing customers default to taxable.

`financial.customer_tax_policies` owns the policy record and its versioned
updates. The VAT flag is independent of withholding-tax eligibility even
though both facts share the same per-customer policy row.

## Billing behavior

During the containment window, `financial.billing_tax_resolution` resolves the
policy from the subscription customer before every recurring postpaid invoice,
prepaid renewal and prepaid threshold decision. When the customer is exempt,
invoice lines carry no tax rate and snapshot the `exempt` application. Manual
invoice authoring keeps its existing exemption consumer. The checkbox is not
the enforcement boundary, and callers do not maintain their own precedence
rules.

Changing the policy affects invoices created afterward. Existing issued
invoices retain their recorded tax treatment and must be corrected through
the normal credit-note or replacement workflow.

`financial.billing_tax_reconciliation` supplies the bounded operator queue for
historical money impact. It confirms an exact candidate only when the invoice
tax point is on or after the current policy row's `updated_at` evidence and all
tax-bearing lines belong to subscriptions. Older invoices and mixed invoices
remain manual-review candidates because the mutable policy row is not a
historical timeline. The former “Prices include VAT” offer label produces only
an ambiguity candidate: current offer state cannot prove the operator's past
intent. Existing issued invoice-linked credit-note tax totals are subtracted
from the displayed exposure. Confirmed corrections are explicitly previewed
and issued through `financial.credit_notes`; the queue never rewrites or
silently recalculates an invoice.

### Credit-note tax identity

Ordinary account credits remain non-tax documents:
`subtotal = total` and `tax_total = 0`. The general credit-note form and
automated commercial-credit callers do not gain a tax amount.

The reconciliation confirmation is the explicit, narrow statutory-correction
case: it creates an invoice-linked tax credit with `subtotal = 0` and
`tax_total = total`, preserves the original source tax-rate reference when the
invoice has exactly one, and records the reconciliation fingerprint in its
memo. Treating this as a non-tax account credit would compensate the customer
without reducing the recorded output-tax liability. Only a confirmed
post-policy, subscription-only candidate may enter this workflow; ambiguous
cases cannot populate `tax_total`. `financial.credit_notes` still owns issue,
funding, application and void evidence, while `financial.tax_accounting` owns
the resulting credit-note tax-recognition projection.

This policy and both containment services retire at the `dotmac-tax` cutover.
VAT then becomes one configured tax code among any number of ordered custom tax
components, and the customer fact is backfilled as an effective-dated,
tax-specific, evidenced classification rather than retained as a product-local
VAT switch.

## Verification

- Policy defaults to disabled and can be enabled independently of WHT.
- Customer forms expose the checkbox for both customer types.
- Draft and recurring invoice construction suppress VAT for exempt customers.
- Prepaid renewal amounts and threshold decisions suppress VAT for exempt
  customers.
- Reconciliation distinguishes confirmed post-policy invoices from unknown
  historical timing and label ambiguity, subtracts existing tax credits and
  refuses stale or ambiguous correction requests.
- The migration backfills all existing customers as non-exempt.
