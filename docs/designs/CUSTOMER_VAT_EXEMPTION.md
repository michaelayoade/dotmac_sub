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
invoices retain their recorded tax treatment. Historical identification,
customer compensation, and statutory output-tax correction remain separate
Finance-reviewed work; this containment change does not infer past exemption
timing, create a reconciliation queue, or issue a correction document.

### Credit-note tax identity

Ordinary account credits remain non-tax documents:
`subtotal = total` and `tax_total = 0`. The general credit-note form and
automated commercial-credit callers do not gain a tax amount.

The current credit-note owner does not support a line-less, header-only tax
correction, and this containment change does not weaken its line, total,
funding, or application invariants. Finance must approve the statutory document
shape and customer-balance, output-tax, cash-basis, reversal, and ERP-posting
consequences before a separate implementation can issue one. A fake taxable
line, a header-only tax amount, or a non-tax account credit is not used as a
substitute for that decision.

This policy and the compatibility resolver retire at the `dotmac-tax` cutover.
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
- Existing issued invoices remain immutable and no automated historical tax
  correction surface is introduced.
- The migration backfills all existing customers as non-exempt.
