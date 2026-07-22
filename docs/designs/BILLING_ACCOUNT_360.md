# Billing Account 360

Status: implemented read-side slice

## Page contract

- **Screen:** `admin.billing.account_detail`; detail/360 page.
- **Audience and job:** finance and support staff answering what the account owes,
  whether prepaid funding is available, and which financial document explains an
  account movement.
- **Primary entity:** canonical Subscriber billing account, identified by account
  number and UUID.
- **Read owner:** `ui.billing_account_workspace_projection` in
  `app.services.web_billing_accounts` and
  `app.services.web_billing_statements`.
- **Authoritative inputs:** account lifecycle from `customer.accounts`; billing
  mode from `financial.billing_profile`; receivables from
  `customer.financial_position`; prepaid funding from the reviewed opening
  position plus native-event owner; statement events from
  `app.services.customer_financial_ledger`.
- **First viewport:** account lifecycle, effective billing mode and provenance,
  outstanding receivables, overdue receivables, prepaid funding availability,
  account identifier, and Customer 360 link.
- **Primary action:** Record Payment. The link opens the existing payment form;
  the payment owner still produces the settlement preview and performs confirmed
  execution. New Invoice and Edit Account remain secondary editor links.
- **Investigation:** the currency-separated account statement is loaded from a
  permission-guarded on-demand fragment; it exposes authoritative source document
  links. Recent invoices and administrative activity remain on the page.
- **Export:** CSV uses the same statement projection and lists one balance lane per
  currency. It never nets unlike nominal currencies.
- **Empty/partial states:** a statement without activity shows an explicit zero in
  the configured default currency. Missing prepaid authority renders
  `Unavailable`; postpaid funding renders not-applicable. Neither becomes zero.
- **Responsive projection:** the four first-viewport facts stack at narrow widths;
  statement balance lanes stack before the evidence table scrolls horizontally.
- **Query budget:** the postpaid first-viewport owner is guarded at no more than
  12 SQL statements, including account identity, recent invoices, billing
  settings, billing profile, and receivable summaries. Statement evidence is a
  separate on-demand investigation read for the selected period.

## Ownership and migration

The former page read `account.balance`, a field the account model does not own,
and therefore rendered a synthetic zero. Its statement also summed every
currency into one opening, activity, closing, and running balance. Both paths are
retired.

The workspace now renders only owner-produced `StateValue`, status presentation,
currency summary, and statement-row projections. Templates format no financial
arithmetic and do not decide whether prepaid funding exists. Money-changing
commands remain with invoice, payment, credit-note, and ledger owners; this page
does not add a writer.

The global `/admin/billing/ledger` migration is intentionally a later slice. It
still needs a scalable cross-account event projection before its hard-coded
legacy cutoff can be retired safely; this account-scoped page does not duplicate
that policy.
