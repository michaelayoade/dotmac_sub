# Future catalogue invoice authoring

## Scope and ownership

Administrative future invoice creation selects server-owned catalogue prices.
Historical gaps, documentary reconstruction and repair activation are excluded.
`financial.invoice_draft_authoring` remains the single complete-draft command
owner. `app.services.future_invoice_authoring` supplies its typed read-only
catalogue, subscription, period and preview evidence. `financial.invoices`
owns line reservations and issuance rechecks; payments and entitlement owners
retain settlement and service-activation authority. The UI never decides dates,
prices, VAT or eligibility.

## Page contract

Screen: admin future invoice editor at `/admin/billing/invoices/new`, with
catalogue-draft edits using the same surface. Audience: finance operators with
invoice create/update permission. Job: select configured charges and future
subscription intervals, review consequences, save one atomic draft.

First viewport: customer/account, currency, due date and catalogue search.
Catalogue results show item name, price and charge basis. One-time items take
quantity; monthly service requires the customer's matching active subscription
and an available exact start/end interval. The date input selects a known
candidate, never arbitrary dates. Calendar labels use the configured display timezone;
persisted instants remain timezone-aware UTC. One period produces one line. Subscriptions with recurring add-on charges are
blocked for affected intervals until all their components can be authored together;
the editor never silently omits those charges.

Capped renewal-cycle quota plans show allowance and validity, not invented
future dates. One pending renewal is permitted per subscription. Their exact
activation interval is determined only from verified settlement through the
existing renewal resolver. Current coverage can legitimately overlap a new
quota validity interval; the normal quota rules are preserved.

Review popup: customer identity, every item/period or pending-renewal package,
quantity, owner-resolved VAT and total. Disclose that saving reserves charges,
not activation. Go back returns focus; confirmation prevents double submit.
Save rechecks current account ownership, lifecycle, pending changes, price/tax
provenance, exact future periods and availability inside the owner transaction.
Expired previews require fresh review. Issuance revalidates persisted selections
against current owners without trusting browser amounts or silently repricing.

Loading/errors: request generation guards prevent stale search/selection
results. Errors remain visible, no fabricated empty-success state. Existing
conflicts name the document reference; unnumbered drafts use their stable ID.
Historical unknowns cannot be selected by this flow. Keyboard controls support
catalogue results, native form controls and modal focus trapping.

## Date and pricing sources

Prepaid future intervals continue from exact uninterrupted funded/granted
coverage using `resolve_prepaid_subscription_settlement_period` and the existing
multi-period purchase cadence. Without a proved future boundary, a lapsed
subscription cannot receive a guessed date before payment. Postpaid preserves
its incumbent sequential period arithmetic and proration rules. Extensions and
pause compensation are source intervals, not handwritten day shifts.

Contracted recurring price and dated discounts override catalogue amount where
the owning resolver specifies that. One-time offers/add-ons retain configured
price and inclusive/exclusive/exempt basis. Customer exemption, account and
service tax precedence remain with `financial.billing_tax_resolution` and
customer-tax-policy owners. All values use Decimal; preview source identifiers
and tax-policy version enter the fingerprint and stored evidence.

## Storage and duplicate prevention

Invoice lines retain optional subscription identity and their existing metadata
periods. New lines carry catalogue item/price identity, a nested typed preview
selection and a strict catalogue reservation marker. Active lines on active,
non-void invoices are the reservation record; no independent expiry clock or
entitlement is created at draft time. Written-off charges remain billed.

Every ordinary line constructor and raw line mutation acquires the canonical
account lock and checks the shared interval/component policy. Exact and partial
overlaps conflict; base service and add-ons retain distinct identities.
Automatic writers cannot bypass a catalogue reservation with a different source
key. Capped pending identity blocks duplicate unsettled requests but ignores
old fully paid cycles, preserving quota restart semantics. Pending purchase
checkouts also block competing authoring. Draft replacement claims all desired
lines before releasing removed ones in the same transaction.

Generic incremental line add/update/delete and standalone invoice-header update
are retired with a stable conflict response. They cannot bypass complete-draft
validation or strip catalogue provenance. Existing legacy drafts remain editable
through the canonical complete-draft owner; new web creation requires catalogue
selections. Internal automation and imported-observation owners remain available
and cannot create a competing service charge against a catalogue reservation.

## Settlement

Monthly line metadata dates take precedence over invoice-header dates when
creating entitlements. Catalogue periods must not be reanchored by the generic
lapsed-payment fallback. Exact funded/granted evidence still controls access;
creating/issuing a document grants no service.

A capped catalogue line requires complete current succeeded-payment allocation
and settlement evidence before its activation dates are resolved. A paid status
label or credit-note-only discharge is insufficient to create a cash-funded
quota renewal. The live allowance/validity must match the quoted package.
The existing quota-cycle initializer creates cycle/baseline evidence inside the
same settlement transaction. Retry reuses the source-line entitlement interval;
refunds retain existing entitlement revocation semantics. The invoice's original
unknown-date financial header is not rewritten to pretend dates were promised
at authoring.

## Starter dependency

See `CATALOGUE_MULTI_PERIOD_STARTER_ADOPTION.md`. Distinct line periods require
the explicit candidate Billing contract/migration and Sub bridge; no old
homogeneous financial guard is removed. Released a8 remains the production pin
until a real candidate release is qualified and its artifact verified. Missing
candidate capability fails closed at catalogue preview, claim admission and
multi-period issuance, rather than at app import or by a new local fallback.
The shared legacy commercial VAT quote API is source preparation in unpublished
Tax a7; Sub still pins released Tax a4. The incumbent subscription VAT resolver
and tax calculator remain unchanged for released-package legacy paths. Local
editable Billing/Tax source is development evidence only; package publication,
qualified artifact adoption and exact consumer pin/lock changes remain separate
release work.

## Validation and rollout

Required: source/owner tests, catalogue tampering and VAT tests, date/coverage
regressions, capped partial/full settlement and quota tests, migrated PostgreSQL
manual/manual and manual/automatic races, fresh and predecessor Billing migration
proofs on both planes, old invoice fact/replay compatibility, actual form and
confirmation behavior, and repository-prescribed static/architecture/integration
checks. Browser acceptance must cover keyboard, customer changes, stale requests,
confirmation cancellation, repeated submit, conflict messages and mobile layout.

No historical importer, repair --apply, production setting, source merge,
publication or deployment is part of this implementation task.
