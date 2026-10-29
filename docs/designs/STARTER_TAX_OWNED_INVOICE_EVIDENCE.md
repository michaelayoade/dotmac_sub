# Native Starter invoice Tax evidence (pending package release)

This draft depends on unpublished Billing a9, including its V2 rated commands,
full Tax evidence mirrors and union historical-fact reader. The current composed
pins remain unchanged. It claims no live activation, package release, CI
acceptance, production policy publication or completed legacy cutover.

The existing `starter_ledger_live_enabled` gate and commercial eligibility bounds
remain authoritative. Discounted, nonpositive, unreconciled, cross-service and
otherwise ineligible drafts retain the existing legacy lane. An eligible draft
must resolve the typed `billing.starter_invoice_tax_policy`; missing policy
refuses inside the native owner, without falling back after eligibility. Tax
writes and Billing issuance participate in the caller's transaction and roll
back together. Native failures never invoke legacy issuance.

## Source and determination owners

Sub supplies commercial pre-tax line amounts (quantity times contractual unit
price), immutable lane source reference/version, invoice expectation date and
counterparty/service/place references. It never supplies a mutable customer WHT
checkbox, legacy numeric rate or an independently computed WHT basis. Tax owns
published rules, effective classifications, calculation bases and rates. Every
public Tax set, component and line field, including source/result fingerprints,
is mapped into Billing's immutable evidence contract. Billing is the original
document evidence owner. Sub's invoice rows retain structural mirrors only.

The typed JSON setting is declared in the existing settings catalog, with its
validator in `TYPED_JSON_SETTING_VALIDATORS` and audited settings-form-only
writes. Its exact shape is:

```json
{
  "invoice_tax": {
    "jurisdiction_id": "<published Tax jurisdiction UUID>",
    "fact_kind": "<published invoice tax source kind>",
    "recognition_basis_code": "invoice_expectation",
    "transaction_side": "output",
    "place_ref": "<immutable place identity>"
  },
  "expected_withholding": {
    "jurisdiction_id": "<published Tax jurisdiction UUID>",
    "fact_kind": "<published withholding expectation source kind>",
    "recognition_basis_code": "invoice_expectation",
    "transaction_side": "withholding",
    "place_ref": "<immutable place identity>"
  }
}
```

These are source bindings, not numeric tax settings. The configured Tax rules
must match this recognition and side. Both purposes require a result, including
an explicit zero/exempt/out-of-scope determination for nontax profiles. A missing
WHT policy never means zero. Tax resolves party/supply/place classification from
the supplied references. No Sub category override is supplied.

The draft supports the existing single-line quantity-one rated composition and
homogeneous multiple-line/per-line-obligation composition, each line with two
independent purpose sets. Invoice-tax net must equal the commercial pre-tax line
amount, and aggregate Tax invoice tax must match the accepted draft tax/total;
otherwise issuance refuses without rewriting the draft. Inclusive source
normalization is outside this pre-tax source slice. WHT supports independent
source-amount policies only. WHT `source_plus_prior_tax` refuses: cross-purpose
VAT/WHT compounding requires a Tax purpose-routing contract, and Sub must never
add VAT to a WHT basis or label VAT components as WHT to work around it. Invoice
tax components may retain Tax-owned compound bases. Fixed, progressive and mixed
rates retain full evidence and never acquire a fabricated simple rate.

## Reads, payment and legacy limits

New single/multiple-line native issues pin their exact original fact version.
The successor reader returns original V1 or V2 according to persisted contract.
V2 presentation displays frozen tax bases/amounts and original expected net;
current balance cards and PDF balance headers still use payment/credit-adjusted
balance. WHT expectation does not reduce the receivable, create accepted proof,
create a tax claim or discharge debt. Current policy edits never rewrite a fact.

V1 documents have no native WHT expectation. Readers never consult today's
customer checkbox/rate to manufacture one. Historical unversioned multi-line
links and legacy invoices keep their existing historical mirror snapshots; no
backfill or new legacy snapshot columns are introduced.

Direct-transfer native WHT reads the same frozen fact and refuses automatic WHT
after partial settlement/adjustments. Positive native WHT then fails closed with
`native_withholding_proof_contract_pending`: the current accepted-proof owner
assumes one numeric percentage and a legacy gross-payment protocol. This draft
does not pass fixed/progressive/mixed evidence through that protocol, invent a
zero rate, or claim proof/discharge migration. Legacy direct transfers retain
the declared legacy policy/proof owners until their separate cutover.

## Remaining gates and validation

Billing a9 must be released, pinned by the integration coordinator, migrated and
qualified on hosted CI before native issuance can use this draft. Published Tax
rules/classifications and audited source bindings must be authored before any
separate switch activation. The generic settings UI binding exists; a dedicated
Tax policy-authoring UI/workflow and live policy publication remain unresolved.

Focused tests cover lossless DTO mapping, both-purpose policy validation, real
native issuance, immutable reads after binding edits, missing-policy refusal,
and current balance after actual Starter payment. Existing native PG fixtures
publish synthetic invoice-expectation Tax rules and explicit zero WHT through
public Tax APIs, with a separate zero-tax fixture for recurring drafts. They use
the repository's migrated PostgreSQL lane, never metadata schema acceptance.
Tests were authored but not executed locally; hosted CI is required.
