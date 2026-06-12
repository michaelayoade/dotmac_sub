## Invoice Tax Inclusion

Invoices always calculate tax from line item tax rates. There is no invoice-level
toggle to include or exclude tax from the invoice total.

- Tax rates are selected per line item.
- Tax is computed from the line items and shown in the invoice summary.
- Invoice total always equals subtotal plus tax.
- Any change to line items recalculates subtotal, tax, total, and balance due.

> Note (2026-06-12): PR #212 (open) adds a configurable **default** VAT rate +
> application mode (`billing.default_tax_rate_id` / `default_tax_application`),
> applied only when no line/address/subscriber rate resolves. It is **off by
> default** (no amounts change until configured); per-line tax computation is
> unchanged. Update this section when #212 merges.
