# Invoice withholding through native financial owners

## Supported operator flow

An eligible customer has withholding enabled by `financial.customer_tax_policies`.
The checkbox beside VAT is unchecked by default. Changing it on a draft records
selection intent only. A versioned compare-and-swap command and database trigger
protect that intent; issuance makes it immutable. The existing Issue button
opens the shared ActionForm confirmation when withholding is selected.

The admin can record one bulk bank deposit and issue drafts gradually across
separate sessions. Each invoice consumes only its required cash; unused credit
remains in the native ledger for later invoices. It does not expire with the
browser or application session. The canonical preview must prove that the new
receipt is wholly unallocated. Its balance-after field intentionally includes
the receipt and is not a no-funding-change test. An enabled auto-allocation
option is harmless when there are no issued invoices to allocate.
Existing debt requires the existing unallocated option rather than bypassing
allocation decisions. Native deposits are limited to the live profile, an
eligible WHT customer and an existing active withholding Tax-code binding with
the required native capabilities. Unconfigured, unactivated accounts keep their
incumbent lane. Once this owner has recorded a native deposit, later receipts
stay native even when customer WHT eligibility changes; disabling the native
lane refuses the receipt instead of redirecting money into a legacy ledger.
The manual payment coordinator delegates a new native deposit to the existing
`starter_manual_settlement` participant. Native Payments records the actual staff
confirmation and native Billing accepts the confirmed cash as available credit.
A bank reference, real collection configuration and operator authority are
required. No legacy Payment or local ledger credit is manufactured for this lane.
The genuine native confirmation ID binds the committed manual receipt event.
Exact replay returns that identity and skips another audit/event. Native Payments
rejects reuse of the same bank reference for a different intent; legacy payment
lookups are not claimed to detect native duplicates.

When the checkbox is unchecked, the existing invoice draft-authoring coordinator
owns the Issue transaction. For an account with an actual qualified native
deposit observation it delegates issuance and applies only that invoice's
required cash through existing native Billing writers, followed by the existing document projection
owner. It uses no WHT. Insufficient cash remains untouched under the incumbent
full-credit Issue policy. A native deposit never silently falls back to a legacy
receivable. Accounts without this new native cash retain existing issue behavior.

## Financial authority

The invoice retains its VAT-exclusive subtotal, VAT and original gross total.
`dotmac-tax` previews and determines withholding from an existing published Tax
policy. The typed fact uses `fact_kind=invoice`,
`recognition_basis_code=invoice_issued`, `transaction_side=withholding` and the
invoice subtotal as its base. The configured jurisdiction/code must exist for the
operator tenant. No rate, legal authority or Tax evidence is invented from the
legacy percentage setting.

`dotmac-billing` computes the cash requirement and accepts the noncash withholding
discharge using genuine Tax determination provenance. The issue coordinator calls
the existing native invoice issuance writer, accepts that discharge and applies
exact native cash credit in one owner transaction. It never creates cash for WHT.
The existing Starter settlement projection owner has a typed document-grained
projection used by this new WHT coordinator. The incumbent provider settlement
path retains its existing account-level collectible projection until separately
qualified package adoption; that known limitation is not silently changed here.

The confirmation binds the invoice, selected version, Tax policy and native cash
position. Account and invoice locks precede revalidation. A changed preview or
insufficient cash rolls back all consequences. Exact replay checks the existing
native discharge and document receivable; different evidence cannot reissue it.
Catalogue and service-period checks remain enforced during issuance.

## Screens and certificate evidence

The shared display projection and PDF keep gross invoice totals visible and show
expected cash/WHT separately. After native acceptance they read accepted cash,
accepted withholding and outstanding balance from the native document fact.
`native_discharge_id` identifies that evidence without pretending it is a legacy
WithholdingTaxRecord. Legacy staff evidence links appear only for legacy records.
Native certificate observations are displayed separately from invoice settlement.
No certificate is treated as received merely because the invoice was issued.
This change does not add a native certificate-authoring screen.

## Boundaries and readiness

The published Sub pins remain Billing a8 and Tax a4. Candidate source changes must
be qualified and released through the normal package process before production
adoption; local source qualification is not a published release or deployment.
Required native read APIs and existing published Tax policy must be available.
The jurisdiction and code settings default to absent and fail closed. Native cash
credit needs verified receipt or independently reviewed opening adoption.
Existing legacy deposits are not silently adopted as native cash.

Customer transfer intents and legacy receipt-based corrections cannot supply
native cash provenance in this change. Those unsupported paths refuse before
financial effects rather than collect a reduced amount that cannot be settled.
Existing legacy gross WHT records remain historical observations and are never
rewritten or added again as a second invoice discharge. Historical gaps and
legacy cash adoption are outside the authorized future-invoice scope.

## Verification

Real Starter PostgreSQL tests exercise tenant/platform document facts and genuine
Tax determination plus cash credit across two invoices. Sub tests must separately
qualify orchestration, selection immutability, stale review, rollback and display.
A test using mocks proves only its declared orchestration boundary. No full-suite
or production-readiness claim follows from an isolated passing test.

## Prepaid catalogue evidence

The administrative policy admits actual advance collection timing only for
future catalogue lines. Monthly lines require the existing owner's verified
start and end dates. A single capped renewal keeps unknown dates until complete
settlement, then the incumbent entitlement/quota owner uses the native settlement
effect instant and configured validity. Mixed capped catalogue drafts remain
rejected by the existing complete-draft owner. Old unproven prepaid drafts and
historical funding reconstruction are not admitted through this policy. Default
recurring and legacy eligibility remain unchanged.

## Draft and issue numbering

Catalogue drafts without an explicitly supplied number keep their stable draft
UUID until issuance. The existing chosen issuer allocates the official number:
Starter Numbering for the native lane or the existing invoice writer for the
legacy lane. This avoids allocating a legacy draft number and then mistaking it
for an operator-supplied number. A manually supplied number is not silently
replaced to force native eligibility.
