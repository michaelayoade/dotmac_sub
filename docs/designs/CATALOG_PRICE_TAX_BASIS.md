# Catalog Price VAT Basis

## Decision

`service_intent.catalog_policy` owns the VAT basis of every catalog price.
`OfferPrice`, `OfferVersionPrice`, and `AddOnPrice` each persist one required
`TaxApplication`: `exclusive`, `inclusive`, or `exempt`.

The stored amount has one unambiguous meaning:

| Basis | Stored amount | Invoice behavior |
| --- | --- | --- |
| `exclusive` | net price before VAT | line subtotal is the amount; VAT is added |
| `inclusive` | gross price containing VAT | VAT is extracted; the invoice total does not increase |
| `exempt` | untaxed price | no rate identity or VAT amount is recorded on the line |

`financial.billing_tax_resolution` continues to decide whether VAT applies and
which active rate applies. `resolve_catalog_price_tax` combines that decision
with the selected price basis. Customer exemption, catalog-offer exemption, or
an unavailable rate always produces an exempt line even when the price says
inclusive or exclusive. A price marked exempt cannot be made taxable by a
tenant default.

Mixed invoices are valid. A net base plan and a VAT-inclusive add-on are rated
independently and then aggregated by the invoice owner. This prevents the same
VAT embedded in the add-on amount from being added a second time.

## Versioning and mutation

VAT basis is billing-critical. The catalog governance owner blocks changing it
on a price attached to a live subscription, using the same controls as cadence,
currency, and price-type changes. Offer-version prices preserve the basis with
the commercial price snapshot. Existing issued invoice lines remain immutable.

## Migration and repair

Migration `640_catalog_price_tax_application` adds non-null columns and
backfills `exclusive`. That is the only safe automatic classification because
it preserves pre-migration behavior. Amount arithmetic is not authoritative
evidence of commercial intent, so the migration does not guess that values
divisible by 1.075 are inclusive.

Finance must review known gross catalog prices after deployment and set them to
`inclusive` through the governed catalog command. Existing invoices are not
rewritten; any historical customer correction remains owned by
`financial.historical_invoice_tax_corrections`.

## Drift and validation

The database `NOT NULL` constraint prevents an unclassified price. API schemas
and both admin catalog forms expose the same typed enum. A missing or invalid UI
selection is rejected before a write. Billing regression tests prove inclusive,
exclusive, exempt, and mixed-line totals. The idempotent repair path for an
incorrect catalog classification is to create the governed replacement
price/version (or update only when no live subscription is attached), then let
future invoices consume that canonical basis.
