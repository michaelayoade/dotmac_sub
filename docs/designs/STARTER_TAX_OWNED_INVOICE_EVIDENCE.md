# Native Starter invoice Tax evidence (pending package release)

This draft depends on verified Tax a7 and Billing a10 releases, following the
Tax a6/Billing a9 foundation. It consumes Tax V2 dependency determinations,
Billing V3 rated commands, full public Tax evidence mirrors and the successor
historical-fact reader. Candidate source compatibility is not release evidence. The current composed
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
    "place_ref": "<immutable place identity>",
    "prior_purposes": ["invoice_tax"]
  }
}
```

These are source bindings, not numeric tax settings. The configured Tax rules
must match this recognition and side. Both purposes require a result, including
an explicit zero/exempt/out-of-scope determination for nontax profiles. A missing
WHT policy never means zero. Tax resolves party/supply/place classification from
the supplied references. No Sub category override is supplied.

The optional `prior_purposes` field defaults to `[]` for existing independent
bindings. Only expected withholding may declare `["invoice_tax"]`. Unknown,
duplicate, reversed and cyclic dependencies refuse during typed policy parsing.
This field routes immutable results; it does not select numeric components.
Tax's published dependency policy chooses `all_prior` or explicit tax code IDs,
checks the exact prior fact signature, and refuses missing prior or selected
component evidence. An explicit zero prior remains a complete result.

For each line, Sub constructs the original commercial identity from the immutable
line source reference/version, shared by its invoice-tax and withholding facts.
Each purpose still has its own source reference. Every line uses its original
commercial amount and classifications; lines never share an invented invoice-wide
identity. Invoice tax is determined first through `determine_tax_set_v2` with no
prior references. Dependent withholding then receives the complete sealed prior
reference. Sub never adds VAT to the withholding input amount. Tax validates the
owner seal and computes the published dependency base. Unsupported signatures,
missing policy and unsupported base combinations propagate as native refusal.

The adapter explicitly copies every public set/component/band field, dependency
policy signature and selection, selected snapshots, and complete referenced
prior results into `InvoiceTaxEvidenceV2`. Source/result fingerprints are opaque
Tax-owned seals: Sub and Billing do not reconstruct the hidden seal preimage.
The V3 obligation declares `commercial_sources` containing the exact principal
commercial identity; a multi-line document retains each line's distinct identity.
Billing freezes the evidence and checks the public graph without becoming a
second Tax calculator.

The draft supports the existing single-line quantity-one rated composition and
homogeneous multiple-line/per-line-obligation composition. Invoice-tax net must
equal the commercial pre-tax line amount, and aggregate invoice tax must match
the accepted draft tax/total. Reconciliation failure refuses issuance without
rewriting the draft. Inclusive source normalization remains outside this
pre-tax source slice. Fixed, progressive and mixed results retain their complete
Tax evidence and never acquire a fabricated simple rate.

## Reads, payment and legacy limits

New single/multiple-line native issues pin their exact original fact version.
The successor reader returns original V1, V2 or V3 according to persisted contract.
V2/V3 presentation displays frozen tax bases/amounts and original expected net;
current balance cards and PDF balance headers still use payment/credit-adjusted
balance. WHT expectation does not reduce the receivable, create accepted proof,
create a tax claim or discharge debt. Current policy edits never rewrite a fact.

V1 documents have no native WHT expectation. Readers never consult today's
customer checkbox/rate to manufacture one. Historical unversioned multi-line
links and legacy invoices keep their existing historical mirror snapshots; no
backfill or new legacy snapshot columns are introduced.

Direct-transfer native WHT reads the same original frozen fact. Original V2
invoices retain `WithholdingTaxEvidenceV1`. Original V3 invoices use
`WithholdingTaxEvidenceV2`, retaining the complete principal
`InvoiceTaxEvidenceV2.determination` and its full `prior_determinations` graph,
including commercial source identity, selected dependency policy/components,
source/result fingerprints and transitive public prior evidence. The adapter
never flattens V2 evidence into V1, substitutes a component subtotal for the
original commercial amount, or reconstructs a rate. The full immutable invoice
fact fingerprint covers this graph; replay rebuilds the same versioned evidence
and compares the complete accepted Billing discharge, without a current Tax read.

The bounded
accepted-proof lane requires exactly one native invoice link on its Billing
account, exactly one original Billing monetary posting version, original full
authoritative collectible, positive exact net cash and
full explicit finance acceptance of every positive frozen WHT component. There
is no public invoice-bound remaining-position reader that would safely loosen
this slice to multiple invoice accounts. The public current position reader
holds the Billing account lock; its single source version also excludes offsetting
cash/reversal/credit history whose net balance could look original. Accounts
with additional monetary history, multiple invoices, and all partial or
adjusted cases refuse before writes. Current policy/rates are never read.

The transfer intent stores a typed schema-2 original fact identity/fingerprint,
account/document references, net/gross amounts and exact Tax component references.
Payments receives the exact persisted checkout binding. Customer submission
links one proof without a synthetic percentage. Staff supplies separate claimed
and accepted amounts per component, plus explicit finance acceptance evidence,
under the existing `billing:proof:verify` capability and authenticated principal.
Expectation alone never creates a claim or acceptance.

`financial.payment_proofs` retains the root transaction and account/proof locks.
Its native branch runs before legacy gross-payment conversion. The existing
Starter settlement owner records actual net cash and allocates that amount only.
A typed required flush-only participant accepts each complete component through
Billing's public `AcceptWithholdingDischarge`, binding original Tax evidence,
proof claim, finance actor/evidence and actual settlement. Only after every
component succeeds does the existing settlement owner mirror the final Billing
position and stage its exactly-once access consequence. A later failure rolls
back cash, discharges, mirrors, notifications and all staged events together.
No legacy Payment, inflated cash, legacy WHT receivable or shadow invoice is
created. Legacy direct transfers retain their declared owners.

The canonical Topup lifecycle participant validates public Payments cash/intent,
the exact active Billing allocation and each immutable Billing discharge before
storing native completion identities in its existing metadata. Native UUIDs never
enter the legacy Payment foreign key. The immutable resolution binds proof,
original fact fingerprint, actual cash, component claims/acceptances and finance
principal; exact proof replay reconstructs the complete checkout snapshot from
the original immutable Billing fact/link, compares its identity and fingerprint
with both checkout and acceptance records, and validates immutable Payments cash
and Billing discharge evidence before returning the original receipt. The
receipt allocation posting group resolves through Billing
`get_allocation_accounting_fact_v1`: its original account, settlement, invoice,
currency, actual amount and posting effects must agree. Native proof allocations
use the exact immutable source reference `proof:{proof UUID}:cash-allocation`,
passed explicitly to settlement; other settlement callers retain their existing
policy source reference. Replay requires that literal proof binding, so a later
real allocation of the same cash to the same invoice cannot replace the original
receipt group. Billing permanently reserves the source reference through its
tenant-scoped `billing.allocation.apply` idempotency key; reversal does not make
it reusable. This is the public command/ledger invariant, not a standalone unique
constraint on the posting-group source-reference column. This immutable historical
read continues to validate the original acceptance after allocation reversal;
replay never uses a current-allocation admission reader. It reads
no current balance or policy and emits no second consequence. Changed replay refuses. Component
identity is accepted once, including after Billing's explicit reverse command;
a new proof cannot manufacture replacement Tax identities. Rejection records no
cash or discharge. Billing remains the reverse-command owner; reversal does not
rewrite the original proof acceptance or immutable invoice fact.

## Remaining gates and validation

Tax a7 and Billing a10 must be independently reviewed, released, pinned by the
integration coordinator, migrated and
qualified on hosted CI before native issuance can use this draft. Published Tax
rules/classifications and audited source bindings must be authored before any
separate switch activation. The generic settings UI binding exists; a dedicated
Tax policy-authoring UI/workflow and live policy publication remain unresolved.

Focused tests cover complete DTO/prior graph mapping, both-purpose policy and
dependency routing validation, real native VAT-to-WHT issuance with all-prior
and selected-code policies, distinct original multi-line identities, exact replay,
retained zero prior evidence, missing-prior/code
refusal, immutable reads after binding edits, participant rollback after Billing
issuance, and current balance after actual Starter payment. Existing native PG fixtures
publish synthetic invoice-expectation Tax rules and explicit zero WHT through
public Tax APIs, with a separate zero-tax fixture for recurring drafts. They use
the repository's migrated PostgreSQL lane, never metadata schema acceptance.
Tests were authored but not executed locally; hosted CI is required.


`tests/integration/test_native_withholding_proofs_pg.py` uses a real Alembic-head
clone with independently committed sessions for concurrent acceptance. It covers
percentage, fixed, progressive and mixed components; exact cash plus withholding
reconciliation; missing/partial/changed claims and cash refusal; late rollback;
exact replay; rejection; original component reuse after reversal; and tenant
isolation. This is authored coverage, not an executed acceptance claim. No local
pytest, dependency installation or Docker was run.


The native proof PostgreSQL canary also issues a real V3 invoice using published
Tax a7 VAT-to-WHT dependency policies with both all-prior and explicit-code
selection. It proves actual cash 102.12 plus accepted withholding 5.38 equals
gross 107.50, retains the complete public principal/prior evidence in the V2
Billing discharge, preserves replay after policy routing edits, and refuses
coordinated checkout/receipt fingerprint drift derived from changed lineage.
Existing reversal and same-cash reallocation substitution canaries remain.
These are authored tests, pending hosted execution on the pinned releases.
