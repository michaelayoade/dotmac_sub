# Catalogue invoice multi-period Billing adoption

Status: source-only candidate; no package publication, consumer-pin change or
runtime activation is claimed by this design.

The paired Starter checkout declares `dotmac-billing==0.1.0a12.dev0` for this
work. It is development source, not a private-index release. The consumer
currently pins released a8. Publication qualification must allocate the final
exact release version (proposed a12, subject to the release captain's canonical
allocation), publish/verify its artifacts and record their hashes before Sub
changes its exact dependency and generated lock. Do not point a production
consumer at a branch, local path or unavailable candidate distribution.

## Contract

For one subscription with several distinct verified periods on one invoice,
Sub builds the existing typed per-line rated obligations but passes each line's
actual `billing_period_start` and `billing_period_end` metadata, with validated
aware instants. Old lines with neither metadata key retain header fallback;
partial or invalid metadata refuses instead of silently using another period.

The paired API is `CreateMultiPeriodDraftDocumentV1` and
`create_multi_period_draft_document_v1`. It preserves one subject, account, currency and timing while allowing verified
nonoverlapping periods per service and explicit NOT_APPLICABLE one-time items.
The old multiline command retains its original homogeneity guard.
Mixed services and one-time items retain each actual line service reference;
one-time lines have no fabricated subscription or period. The aggregate
exposure has no single service reference when lines refer to different services.

`get_invoice_service_periods_v1` provides exact original immutable line and
obligation identities, dates and amounts. It reports document-grained
settlement and provides no invented paid amount for an individual month.
Aggregate receivable exposure does not falsely use the first month as the
whole document's service period. Unknown validity boundaries stay unknown;
this API does not infer an entitlement from a capped preview interval.

## Qualification sequence

1. Complete the paired Starter source review, fresh migration and actual bi12
   predecessor-to-bi13 PostgreSQL tests on both tenant/platform planes. Preserve
   the published predecessor migration bytes and historical original facts.
2. Qualify bi13 in the module release policy and follow its security/release
   authority controls. A dev package version is not this authorization.
3. Build once, verify exact released artifact identities, install back and
   record publication through the prescribed release workflow.
4. Update Sub's exact pin/lock and migration-composition contract together;
   test real deployed-schema predecessor upgrade plus per-line facts, invoice
   rendering, partial settlement and replay. Run the normal full required CI.
5. Deploy only through the existing named-target immutable digest staging and
   production authorization process. This source task performs no deployment.

Before adoption, the normal released a8 imports continue to work. Attempting
a distinct-period issue lazily requires the new API and raises
`multi_period_billing_unavailable` if absent; it never routes the refused
invoice through a legacy financial writer.
