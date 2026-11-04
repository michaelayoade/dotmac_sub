# Customer account statement (web portal)

Status: implemented on the customer-statement feature branch; hosted CI pending.

## Customer contract

- The authenticated customer can request a PDF for their own account only. The
  customer web portal is the first surface; the mobile app is a later slice.
- The date selector defaults to the last 30 days and accepts any valid older
  date. Dates use the portal's configured display timezone. There is no
  12-month limit. The PDF can be downloaded or emailed to the
  signed-in customer's verified email address as an attachment.
- The statement follows the admin customer-ledger column layout where useful:
  entry, type, source, debit, credit, running balance, description, and date.
  The admin ledger and its existing contract do not change.
- Show **History before the reviewed opening** and **History after the reviewed
  opening** as distinct sections. The pre-opening section explicitly says
  **Unavailable**; no archived transaction rows are displayed. The
  post-opening section starts at the reviewed opening and follows canonical
  native financial events. When the selected date range begins later, its
  period opening carries forward intervening canonical events.
- Keep amount owed and prepaid credit separate. Do not net different currencies
  or conceal an unpaid invoice behind prepaid credit. These two summaries are
  labelled **Current**, since they reflect the account when the PDF is made;
  the selected period's opening, activity, and closing are separate figures.
- Do not claim that the statement contains the unavailable older history or
  infer missing rows.

## Ownership boundary

The customer-statement read owner selects the customer's account, date
window, reviewed opening, and canonical later events. PDF rendering is
presentation of that typed result. Download and email routes are adapters.
The archive is not read or imported for this feature.

The email owner queues only to the signed-in customer's verified email. The
communication attachment reader checks the address, principal, and selected
account binding again at delivery, then generates the PDF for the requested
date range. If the
address changes or is no longer verified, delivery fails closed. An emailed
PDF can therefore include a later recorded correction within the selected
period; its generated date makes that visible. Email attachments above the
communication size limit are refused and remain downloadable.

The current `customer.financial_position` and admin ledger remain authoritative
for their existing concerns. This design does not authorize a production import,
deployment, or change to the admin ledger's source-of-truth declaration.
