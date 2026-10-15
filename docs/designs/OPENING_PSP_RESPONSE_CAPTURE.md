# Opening PSP response-derived capture

Status: source-only candidate; no capture or qualification acceptance.

`integration.runtime` retains transport authority. The typed
`payment_capability.capture_transaction_record` query composes the existing
`payments.reconcile.v1` `verify` operation. It selects an exact reconciliation
binding and installation, requires the current immutable configuration revision
and digest to match the query, and uses the existing pinned runner and secret
materialization. A fresh scalar SQL projection bypasses cached ORM identities;
the canonical installation configuration digest validates its actual configuration,
secret references and schema version before materialization. Relevant dirty ORM
attributes and untracked in-place JSON changes refuse without overwriting caller
state. The validated snapshot becomes detached typed ORM values consumed directly
by the existing runtime builder helper, without re-querying cached values, adding
objects to a session, opening another session or creating another authority.
Revision ownership must match that same installation before
runtime or secret access; a cross-linked revision refuses as `UNBOUND` even if
its ID/digest match the query. Database read exceptions, including an aborted
caller transaction, propagate without rollback or conversion into a refusal.
It never resolves an enabled default or changes financial state.
Session autoflush is disabled for the entire read. There is no paid-status read,
Finance account mapping, confirmation, settlement or reconciliation consequence.

`PaymentCaptureQuery` supplies exact expected major-unit amount and currency.
`ProviderRecord` is an explicit **response-derived sanitized provider record**,
never original response bytes, a webhook, or a signature-qualified receipt.
The existing HTTP adapter parses capture-only JSON decimal numbers as Decimal;
its allowlist retains actual transaction ID/reference/status, strict envelope
status separately, provider-native amount/fee units and fee field name, optional
provider timestamps, and actual UTC retrieval time. Missing fees remain unknown.
Provider timestamps never substitute for acquisition time. Exact reference,
amount and currency mismatches refuse without returning a candidate record.
Customer/email, authorization, metadata, response messages, headers, credentials
and arbitrary payload fields are excluded. JSON serialization occurs only at
the connector adapter seam. Legacy verification and charging contracts are unchanged.

The installation/configuration models and current payment manifests contain no
independently attested provider merchant namespace. A frozen
`AssertedMerchantNamespace` therefore records a **caller assertion**, its UUID
reference and exact installation/configuration binding, separately from provider
observations. Missing or mismatched assertion returns `UNBOUND` before database,
secret or provider access. The namespace assertion is never sent to the provider,
interpreted as a response merchant field, or promoted into a Finance grant.
Neither supported response projection claims provider-observed merchant identity.
An assertion does not establish historical merchant custody or prove that current
credentials represent the historical merchant. Independent Finance/installation
namespace enrollment and trusted issuer policy remain mandatory qualification gates.

This query returns an unqualified candidate only. Fresh verification proves the
current transaction record, not complete refund/dispute history or bank payout.
It does not qualify source cash, reconstruct original issued invoices, sign or
retain artifacts, create a custody store, import openings, or switch authority.
Missing original invoices remain manual exceptions. Files a5 supplies neither
the required immutable version/provenance receipt nor JSON admission contract.
Payments a2 confirmation lacks retained-evidence fields. A later genuine Starter
rehearsal consumer must compose released qualification contracts with real retained
custody and independent merchant/issuer policy; draft Billing 845 is not copied or
made a dependency here.

Focused canaries use existing connector helpers and mocked HTTP only, covering
envelope failure, distinct transaction status, exact decimal preservation,
binding/default refusal, asserted namespace separation, amount/currency/reference
mismatches, credential/PII exclusion and database-write traps including autoflush.
They require hosted execution. Registry/map integration, independent review,
hosted full prescribed checks, concrete source/growth approval and independently
qualified retained evidence remain gates. No operational readiness is claimed.
