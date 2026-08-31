# Sub tax-fact adapter, outbox and classification backfill (ledger C5)

Status: design and containment specification. Not a cutover, not a backfill run,
and not production evidence. No writer changes are authorized by this document.

Sub source revision: `df970604c23d89546b949d8fb28e5230ca61ade7` (`origin/dev`).
Contract source: `dotmac-tax` at Starter
`packages/dotmac-tax/src/dotmac_tax/{contracts,models,service}.py`; the ledger
records the package at `0.1.0a2`, and ledger B8 (publication, registry install,
peeled tag) is still open, so no version is pinned here.

Programme ledger: Starter
`docs/superpowers/plans/2026-08-23-catalog-variant-decomposition.md`, task C5:
"Define Sub's versioned billing-fact adapter/outbox after ERP authority is
proven. Backfill CustomerTaxPolicy into evidenced classifications and map
offer/service facts to supply and place refs."

Every claim about current Sub behaviour below cites `file:line` at the source
revision above. Claims about the contract cite the package path.

## 1. Gate: what must be true before any of this is built

C5 is a design slice only. Implementation is blocked on all of:

1. **ERP authority proven** — ledger C1-C4. `dotmac-tax` becomes the statutory
   owner in ERP first; Sub never becomes a second tax owner. The ERP adoption
   boundary document named by ledger C1
   (`docs/architecture/dotmac-tax-adoption-boundary.md` in the ERP repository)
   does not exist at ERP `0f4b1698`, so the shared vocabulary this adapter must
   reuse (`fact_kind`, `recognition_basis_code`, `treatment_code`, authority /
   jurisdiction / tax-code identities) is not yet published. Section 4's
   vocabulary is therefore a *proposal to be reconciled against ERP*, not a
   decision.
2. **`dotmac-tax` release evidenced** — ledger B6-B8. A repository-local version
   string is not evidence of a published, installable, pinnable package.
3. **A named jurisdiction and tax-code identity** — see section 3.1. Sub has no
   jurisdiction concept at all today.
4. **Operator adjudication of the offer-level VAT flags** — section 6.3. This is
   the finding that decides whether Sub's "with VAT / without VAT" offer pairs
   are a *supply* classification or a *customer* classification. It cannot be
   derived from the data.

## 2. What Sub owns today (as-built)

### 2.1 The single compatibility resolver

`app/services/billing_tax_resolution.py` is the one containment owner
(ledger A3), registered as `financial.billing_tax_resolution` at
`app/services/sot_registry/domains/financial_access/invoicing_tax.py:1182-1332`
with `AuthorityMigrationState.COMPLETE` and an explicit retirement note:
"retired when dotmac-tax cuts over"
(`invoicing_tax.py:1319-1325`).

Its precedence, in order (`app/services/billing_tax_resolution.py:158-206`):

| # | Fact | Source | Line |
|---|---|---|---|
| 1 | customer VAT exemption | `CustomerTaxPolicy.vat_exempt` | `billing_tax_resolution.py:114-123,166-167` |
| 2 | service-address rate | `Address.tax_rate_id` | `billing_tax_resolution.py:132-143,169-173` |
| 3 | account rate | `Subscriber.tax_rate_id` | `billing_tax_resolution.py:124-131,174-178` |
| 4 | catalog percent match | `CatalogOffer.vat_percent` matched against active `TaxRate.rate` | `billing_tax_resolution.py:79-96,189-194` |
| 5 | catalog taxable default | `CatalogOffer.with_vat` or positive `vat_percent` -> configured default rate | `billing_tax_resolution.py:195-203` |
| 6 | catalog offer exempt | neither flag set | `billing_tax_resolution.py:204-205` |
| 7 | configured default | `billing.default_tax_rate_id` setting | `billing_tax_resolution.py:52-65,182-187` |

Inclusive/exclusive/exempt treatment is a single tenant-wide setting,
`billing.default_tax_application` (`billing_tax_resolution.py:68-76`), applied
uniformly to whatever rate the precedence selected
(`billing_tax_resolution.py:212-214`).

### 2.2 The three reader paths

- **recurring / postpaid**: `app/services/billing_automation.py:57-59,431`,
  applying `tax_rate_percent` and `tax_application` at
  `billing_automation.py:590-621,656-657`.
- **prepaid renewal**: `app/services/prepaid_service_renewals.py:86,361`.
- **manual / one-off**: `app/services/invoice_draft_authoring.py:45,429,442`
  and `app/services/billing/invoices.py:2515-2519`, which consult
  `CustomerTaxPolicy.vat_exempt` directly and drop `tax_rate_id` when exempt.

A fourth, operator-facing decision surface reads the offer flags directly:
`app/services/web_catalog_calculator.py:82-93,108-137,206-207` (quote and
first-bill estimate). Its docstring records the whole-subscription rule: "VAT
applies to the *whole* taxable base ... invoicing tags every line of a taxable
subscription with the same tax rate"
(`web_catalog_calculator.py:114-121`).

### 2.3 Persisted tax state

| Table / column | Role today | Path |
|---|---|---|
| `tax_rates` (`TaxRate`) | free-form local rate catalogue; name, code, `Numeric(6,4)` rate, `is_active` | `app/models/billing.py:2376-2396` |
| `customer_tax_policies` (`CustomerTaxPolicy`) | one row per account; `vat_exempt`, `withholding_tax_enabled`, one shared `version` counter | `app/models/customer_tax_policy.py:11-53` |
| `catalog_offers.with_vat` / `.vat_percent` | per-offer taxable flag and percent | `app/models/catalog.py:566-567` |
| `subscribers.tax_rate_id` | account-level rate override | `app/models/subscriber.py:340` |
| `addresses.tax_rate_id` | service-address rate override | `app/models/subscriber.py:721` |
| `invoice_lines.tax_rate_id` / `.tax_application` | immutable snapshot on the issued document | `app/models/billing.py:1142-1146` |
| `invoices.tax_total` | document total | `app/models/billing.py:617` |
| `billing_contract_lines.tax_treatment_code` | unused free-text treatment slot | `app/models/billing_contract.py:423-450` |

### 2.4 The clone-forcing guard

`_OFFER_CRITICAL_FIELDS` includes `"with_vat"` and `"vat_percent"`
(`app/services/catalog_billing_governance.py:42-54`), and
`_OFFER_LIVE_IMMUTABLE_FIELDS = _OFFER_CRITICAL_FIELDS - {"is_active",
"status"}` (`catalog_billing_governance.py:56`). Any change to those two fields
on an offer with at least one live subscription is refused
(`catalog_billing_governance.py:200-210`). This is the mechanism that forces an
operator to clone a plan to change its VAT treatment, which is exactly the
duplication this programme removes. Section 8 states when it may be removed.

## 3. Contract shape the adapter must satisfy

`dotmac_tax.contracts.TaxFact` (`contracts.py:62-78`) has 13 fields; nine
selection-relevant ones and four provenance ones. The determination entry point
is `dotmac_tax.service.determine_tax_set(db, *, tenant_id, fact, determined_at)`
(`service.py:786-...`).

Three contract behaviours drive the whole design:

1. **One fact carries exactly one `transaction_side`.** Rule selection filters
   on `TaxRule.transaction_side == fact.transaction_side`
   (`service.py:619`). Output VAT and withholding are therefore two facts, never
   one.
2. **`base_amount` must be non-negative** (`service.py:776-777`) and its
   `Currency` must match the jurisdiction's `currency_code` *and* `minor_units`
   exactly (`service.py:116-119,770-775`).
3. **A direct `*_category` that disagrees with an owned classification is a
   hard conflict.** `_subject_classification` raises `TaxConflict` when both a
   direct category and a classification row exist and differ
   (`service.py:596-599`). Sending a category *and* a ref is a landmine.

`(tenant_id, source_ref, source_version)` is unique on
`tax_determination_sets` (`models.py:320-325`); a replay with the same pair and
a different fingerprint raises `TaxConflict`
(`service.py:827-829`), and a matching replay returns the existing set
(`service.py:827-830`). That is the idempotency contract Sub's outbox binds to.

### 3.1 Jurisdiction

Sub has no jurisdiction, authority or tax-code concept: `TaxRate`
(`app/models/billing.py:2376-2396`) is a bare name/rate row. `Invoice.currency`
is a `String(3)` defaulting to `"NGN"` with no minor-unit column.
`CatalogOffer.region_zone_id` -> `RegionZone` (`app/models/catalog.py:345-353`,
`554-556`) is a commercial availability zone, not a tax place.

Therefore `jurisdiction_id` comes from configuration, not from data: one new
`billing`-domain setting `tax_jurisdiction_id`, resolved through
`settings_spec.resolve_value` exactly as `default_tax_rate_id` is today
(`billing_tax_resolution.py:52-65`). A second setting `tax_vat_code_id` names
the VAT `TaxCode` the classifications attach to (classifications are
tax-code-specific: `models.py:246-260`). Both fail closed: no jurisdiction, no
fact emitted, and the outbox row is rejected with a durable reason rather than
guessed.

## 4. Field-by-field mapping: Sub -> `TaxFact`

The emitted unit is one **fact group**: `(invoice, tax_rate_id,
tax_application)`, ordinally numbered within the invoice. Section 6.1 explains
why the invoice line is the wrong unit.

| `TaxFact` field | Sub source | Rule | Cited path |
|---|---|---|---|
| `jurisdiction_id` | configuration, not data | `billing.tax_jurisdiction_id` setting; absent -> reject, never default | section 3.1 |
| `occurred_on` | `Invoice.issued_at` | date in a declared tenant timezone (see 6.5); draft invoices emit nothing | `app/models/billing.py:626` |
| `fact_kind` | constant per document family | `subscription_service_supply`, `one_time_charge`, `customer_credit_note` | proposal, section 4.1 |
| `recognition_basis_code` | constant | `invoice_issue` for every family; Sub recognises tax at document issue on both postpaid and prepaid paths | `billing_automation.py:590-621`, `prepaid_service_renewals.py:361` |
| `transaction_side` | constant | `output`. Withholding is a separate family and is NOT emitted in C5 (6.4) | `service.py:619` |
| `base_amount` | `Money(sum(InvoiceLine.amount) - apportioned Invoice.discount_amount, Currency(Invoice.currency, minor_units))` | minor units from `dotmac_kernel.money.currency()`; unknown code -> reject | `app/models/billing.py:1141,598,602` |
| `source_ref` | `sub:invoice:{invoice_id}:tax_group:{n}` | `n` = ordinal of the group sorted by `(tax_rate_id, tax_application)`; stable because an issued invoice is immutable | `app/models/billing.py:1117-1146` |
| `source_version` | `{payload_contract_version}.{fact_revision}` | `fact_revision` is the outbox's own monotonic counter per `source_ref`; increments only when the content fingerprint changes | section 5 |
| `evidence_ref` | `sub:invoice:{invoice_id}#{invoice_number}` | `Invoice.invoice_number` is nullable, so the id is the primary anchor and the number is decoration | `app/models/billing.py:594` |
| `party_category` | **always `None`** | see 6.2 — Sub has no trustworthy party tax category | `app/models/subscriber.py:483-509` |
| `counterparty_ref` | `sub:subscriber:{Subscription.subscriber_id}` | the account `CustomerTaxPolicy` keys on; NOT `party_id`, which is nullable | `app/models/customer_tax_policy.py:25-30`, `app/models/subscriber.py:255-259` |
| `supply_category` | **always `None`** | see 6.3 — the only supply-side tax fact Sub holds is `with_vat`, and it must be adjudicated, not auto-mapped | `app/models/catalog.py:566-567` |
| `supply_ref` | `sub:offer_version:{Subscription.offer_version_id}` when set, else `sub:catalog_offer:{Subscription.offer_id}` | offer version is the immutable sellable shape; `offer_version_id` is nullable | `app/models/catalog.py:901-906` |
| `place_code` | **always `None`** | Sub holds a *rate* on the address, never a place classification | `app/models/subscriber.py:721` |
| `place_ref` | `sub:address:{Subscription.service_address_id}` when set, else `None` | `service_address_id` is nullable; a rule that depends on place must not be published while nulls exist | `app/models/catalog.py:907-909` |

### 4.1 Vocabulary is a proposal, not a decision

`fact_kind`, `recognition_basis_code`, `treatment_code` and every category code
are open registered strings matched by exact equality during rule selection
(`service.py:611-620`). ERP publishes the statutory rules first (ledger C4), so
ERP's vocabulary wins. Sub's adapter must reconcile against the ERP boundary
document before a single rule is published, or Sub emits facts that select no
rule and `_applicable_rules` raises `TaxRuleViolation("no applicable tax
rule")` (`service.py:626`).

## 5. Outbox and versioning

### 5.1 Shape

A new tenant-scoped table `tax_fact_exports`, modelled directly on the existing
proven transport `erp_billing_exports`
(`app/models/erp_billing_export.py:55-103`) rather than inventing a second
export idiom:

| Column | Purpose | Precedent |
|---|---|---|
| `id` | identity | `erp_billing_export.py:65-67` |
| `family` (enum) | `subscription_service_supply` / `one_time_charge` / `customer_credit_note` | `ErpBillingFlow`, `erp_billing_export.py:35-43` |
| `source_kind`, `source_id` | `invoice_tax_group`, invoice id | `erp_billing_export.py:71-72` |
| `group_ordinal` | the `n` in `source_ref` | new |
| `source_ref` | the exact string sent as `TaxFact.source_ref` | new; mirrors tax's own uniqueness key |
| `fact_revision` | monotonic per `source_ref`, starts at 1 | new |
| `payload_contract_version` | adapter contract version, starts at 1 | `payload_version`, `erp_billing_export.py:75` |
| `payload` (JSONB) | the serialized `TaxFact` | `erp_billing_export.py:76` |
| `payload_fingerprint` | sha256 over the canonical payload | `TaxDeterminationSet.source_fingerprint`, `models.py:374` |
| `idempotency_key` (unique) | `tax-fact:{source_ref}:{source_version}` | `erp_billing_export.py:60,77` |
| `status` | `pending` / `delivered` / `acknowledged` / `rejected` | `ErpExportStatus`, `erp_billing_export.py:46-52` |
| `attempts`, `last_error` | delivery evidence | `erp_billing_export.py:84-85` |
| `determination_set_ref` | tax's accepted set identity, recorded on acknowledgement | `erp_reference`, `erp_billing_export.py:89` |
| `command_id`, `correlation_id` | provenance | `erp_billing_export.py:91-94` |

### 5.2 Versioning rule

`source_version = f"{payload_contract_version}.{fact_revision}"`.

- `payload_contract_version` changes only when the adapter's serialization
  contract changes, and a change requires re-emitting the affected cohort under
  an explicit migration; it is never bumped silently.
- `fact_revision` increments **only** when a new payload for an existing
  `source_ref` has a different `payload_fingerprint`. An identical re-emission
  reuses the same `source_version`, so `determine_tax_set` returns the existing
  set instead of conflicting (`service.py:827-830`).
- A *different* fingerprint under the *same* `source_version` is a defect, not a
  correction: tax raises `TaxConflict("tax source version was reused with
  different facts")` (`service.py:827-829`). The outbox must detect this before
  emission by comparing fingerprints locally and refusing to overwrite.

### 5.3 Idempotency key

`idempotency_key = "tax-fact:{source_ref}:{source_version}"`, unique-constrained
in Sub, exactly as `uq_erp_billing_export_idempotency`
(`erp_billing_export.py:60`). It is deliberately *not* a hash: an operator
reading the pending queue must be able to see which invoice and which revision a
stuck row belongs to. The content hash lives separately in
`payload_fingerprint` so key identity and content identity cannot be conflated.

### 5.4 Staging discipline

Rows are staged inside the invoice-issue owner command's transaction and
delivered after commit by the durable dispatcher, per `AGENTS.md` ("Domain
events are staged transactionally with the authoritative state change. Delivery
happens after commit through the durable dispatcher/outbox"). Tax being
unreachable never rolls back a Sub invoice — the row simply stays `pending`,
matching the ERP export's stated posture (`erp_billing_export.py:8-11`).

## 6. Findings: Sub facts that do NOT map cleanly

These are reported, not bent to fit the contract.

### 6.1 An invoice-level discount breaks the per-line base

`Invoice.discount_amount` / `discount_type` / `discount_value` sit on the
invoice, guarded by a check constraint
(`app/models/billing.py:556-562`), while `InvoiceLine.amount`
(`app/models/billing.py:1141`) is not rewritten when a discount is applied. So
`sum(line.amount) != invoice taxable base` whenever a discount exists, and the
per-line unit has no correct base. That is why the emitted unit is the
`(invoice, tax_rate_id, tax_application)` group with a declared apportionment
rule, and why the apportionment rule and its rounding residual assignment must
be written down and tested before emission — not chosen at implementation time.

### 6.2 There is no queryable party tax category

`Subscriber.category` is **not a column**. It is a property over the
`metadata_` JSONB key `subscriber_category`
(`app/models/subscriber.py:483-509`) which silently returns
`SubscriberCategory.residential` both when the key is missing and when its value
is unrecognised (`subscriber.py:486-492`), and whose setter silently coerces an
unknown string to `residential` (`subscriber.py:494-505`). A value that cannot
distinguish "residential", "unset" and "corrupt" is not admissible as a tax
input. `TaxFact.party_category` therefore stays `None` and the party decision is
carried entirely by `counterparty_ref` plus published classifications.

### 6.3 The "supply" classification is really the offer clone, and its meaning is ambiguous

The only supply-side tax fact Sub holds is `CatalogOffer.with_vat` /
`vat_percent` (`app/models/catalog.py:566-567`) — the exact fields whose
immutability forces the clone (`catalog_billing_governance.py:42-56`). Two
readings fit the same data and the data cannot choose between them:

- **supply reading**: the service itself is exempt or zero-rated, so the pair of
  offers encodes a genuine supply classification;
- **party reading**: the service is standard-rated and the "without VAT" clone
  exists only because a particular *customer* is exempt and the operator had no
  other way to express it.

The ledger's fixed decision ("Product/service catalogue rows own sellable and
technical shape, not tax policy") says the party reading is the expected answer
for most rows, but "expected" is not evidence. Each `with_vat=false` offer must
be adjudicated by an operator against its live subscriptions before any supply
classification is published. Auto-deriving `supply_category` from `with_vat`
would launder a duplication defect into a statutory classification.

Note also that `plan_category` (`app/models/catalog.py:158-162,579-583`),
`service_type` (`catalog.py:27-29,542`) and `plan_family`
(`catalog.py:588`; vocabulary in `docs/PLAN_FAMILY_ARCHITECTURE.md:21`) are
**not** read by any tax path today — `billing_tax_resolution.py:189-196` reads
only `with_vat` and `vat_percent`. Promoting any of them to a tax input would be
new behaviour introduced during a migration, which this programme forbids.

### 6.4 `withholding_tax_enabled` has no reader and must not acquire one here

`CustomerTaxPolicy.withholding_tax_enabled`
(`app/models/customer_tax_policy.py:31-35`) is written by
`app/services/customer_tax_policies.py:172-216` and read only by the admin
display and form (`app/web/admin/customers.py:266-267,1928,1985,2071,2097`;
`app/services/web_customer_actions.py:224,2550,2631`). No billing, invoicing or
payment path consults it. Withholding in Sub is *proof-backed after the fact* —
`WithholdingTaxRecord` and its evidence timeline
(`app/services/tax_accounting.py:1-8,28-32`), which Sub explicitly owns.

Consequence: emitting a `transaction_side="withholding"` fact in C5 would create
a determination that does not exist today and would change money. C5 publishes
`withholding_tax_enabled` as a *classification* on a WHT tax code with
`basis_code="operator_declaration"` and publishes **no withholding facts and no
withholding rules**. Wiring withholding to a determination is a separate, named
slice with ERP as authority.

### 6.5 `occurred_on` has no declared timezone

`Invoice.issued_at` is `DateTime(timezone=True)` and `TaxFact.occurred_on` is a
`date` (`contracts.py:65`). Converting in UTC puts a 00:30 WAT invoice in the
previous VAT period. The conversion timezone must be a declared, checked-in
configuration value, not an implicit `astimezone(UTC)`.

### 6.6 A credit note cannot be a negative fact

`base_amount` must be non-negative (`service.py:776-777`), but Sub issues
proration credit notes with their own tax amounts
(`app/services/billing_automation.py:2588-2620`). A credit note is therefore its
own positive fact under `fact_kind="customer_credit_note"`, netted at reporting
time through `StatutoryReportBoxInput.multiplier`
(`contracts.py:98-104`). It is never a negative supply fact and never rewrites
the original invoice's determination.

### 6.7 Currency carries no minor units

`Invoice.currency` and `OfferPrice.currency` are bare `String(3)` defaulting to
`"NGN"` (`app/models/billing.py:598`;
`app/models/catalog.py:767`), while the contract requires an exact
`Currency(code, minor_units)` match against the jurisdiction
(`service.py:116-119`). The adapter resolves minor units through
`dotmac_kernel.money.currency()` and **rejects** any code absent from that
registry rather than assuming two decimals.

### 6.8 Inclusive/exclusive moves from a tenant switch to a rule version

Today one setting, `billing.default_tax_application`, flips inclusive/exclusive
for the whole tenant (`billing_tax_resolution.py:68-76`). In the contract,
`inclusive` is a property of a published `TaxRule` version
(`contracts.py:51`; `models.py` `TaxRule.inclusive`), and an inclusive rule
may not be combined with any other component
(`service.py:718-722`). After cutover, changing inclusive treatment is a new
rule version with its own effective date — the tenant-wide toggle disappears,
and any operator workflow that relies on flipping it must be retired in the same
slice.

## 7. Backfilling `CustomerTaxPolicy` into classifications

Target: `dotmac_tax.contracts.TaxSubjectClassificationInput`
(`contracts.py:81-94`) published via
`dotmac_tax.service.publish_tax_subject_classification`
(`service.py:477-...`), persisted as `TaxSubjectClassification`
(`models.py:246-311`).

### 7.1 Field mapping

| Classification field | Value | Note |
|---|---|---|
| `tax_code_id` | `billing.tax_vat_code_id` setting (VAT); a separate WHT code for 7.4 | classifications are tax-code-specific (`models.py:266-272`) |
| `subject_kind` | `"party"` | `models.py:272-275` allows `party` / `supply` / `place` |
| `subject_ref` | `sub:subscriber:{CustomerTaxPolicy.account_id}` | must equal the `counterparty_ref` in section 4 exactly, or the lookup misses (`service.py:577-591`) |
| `category_code` | `exempt_customer` when `vat_exempt` is true | see 7.2 — no row is published for the default state |
| `version` | adapter-owned counter starting at 1 | **not** `CustomerTaxPolicy.version` — see 7.3 |
| `effective_from` | `created_at::date` when `version == 1`, else `updated_at::date` | see 7.5 — earlier history is unrecoverable |
| `effective_to` | `None` | the current state is open-ended |
| `basis_code` | `operator_declaration` when `updated_by` names a real actor, else `legacy_state_snapshot` | see 7.6 |
| `evidence_ref` | `sub:customer_tax_policy:{policy_id}#vat.{version}` | see 7.7 — Sub holds no exemption certificate |
| `published_by_ref` | `sub:actor:{updated_by}` or `sub:system:legacy_backfill` | see 7.6 |
| `source_ref` | `sub:customer_tax_policy:{policy_id}` | |
| `source_version` | `vat.{CustomerTaxPolicy.version}` | prefix is load-bearing — see 7.3 |

### 7.2 Only the exempt state is published

`vat_exempt` defaults to `false` (`app/models/customer_tax_policy.py:36-40`) and
the resolver treats false as "fall through to the next precedence step"
(`billing_tax_resolution.py:166-168`), i.e. not a decision. Publishing a
`standard_rated_customer` row for every account would create tens of thousands
of rows asserting a classification nobody made. Absence of a row means "never
classified", and rule selection already handles that: `_optional_match` treats a
rule with `party_category = None` as matching anything
(`service.py:563-565`). Un-exempting an already-exempt account publishes version
2 with `category_code="standard_rated_customer"` and an explicit basis.

### 7.3 `CustomerTaxPolicy.version` is a shared counter and cannot be the source version

The same `version` column is incremented by both the withholding write
(`app/services/customer_tax_policies.py:206`) and the VAT write
(`customer_tax_policies.py:254`). A withholding flip therefore
bumps the number Sub would otherwise use as VAT provenance, and the VAT and WHT
classifications for one account would collide on
`uq_tax_subject_classifications_source` (`tenant_id, source_ref,
source_version`; `models.py:261-266`) if both used the bare counter.

Two consequences, both mandatory:

1. `source_version` is prefixed per tax (`vat.` / `wht.`) so the two families
   cannot collide on one `source_ref`.
2. The classification `version` — which the package requires to be exactly
   `max + 1` (`service.py:538-540`) — is a **separate adapter-owned counter per
   `(tax_code_id, subject_kind, subject_ref)`**, never `CustomerTaxPolicy.version`.
   A shared counter would skip integers and be rejected.

### 7.4 Withholding classification, no withholding rule

`withholding_tax_enabled` is published as `subject_kind="party"` on the WHT tax
code with `category_code="withholding_applies"`, `basis_code=
"operator_declaration"`, and `source_version="wht.{version}"`. Per 6.4, no
withholding `TaxRule` is published and no withholding fact is emitted, so the
classification is inert evidence until a later named slice wires it. Publishing
the classification now is what makes that later slice a *read* change rather
than another backfill.

### 7.5 Effective dates before the backfill are unrecoverable

`customer_tax_policies.py` stages **no audit event** — the file contains no
`stage_audit_event`, no event emission, and no history table exists
(`alembic/versions/419_customer_wht_policy_and_direct_targets.py:101-160` creates
only `customer_tax_policies`). The row keeps the current value, a counter, and
`created_at` / `updated_at` (`app/models/customer_tax_policy.py:41-51`). If an
account was exempted in March and un-exempted in June, only the June state and
its timestamp survive.

The backfill therefore declares a **blind window**: for any policy with
`version > 1`, everything before `updated_at::date` is unknown. C6 shadow
comparison must either restrict itself to periods after each row's
`effective_from`, or explicitly record the pre-window rows as known-blind and
exclude them from the zero-drift gate. It must not silently backdate
`effective_from` to `created_at` for a row that has changed, because that
asserts a history Sub does not have.

Additionally, the admin adapter reuses one idempotency key per *value*:
`f"customer-vat-exemption:{account_id}:{enabled}"`
(`app/services/web_customer_actions.py:2648-2662`). Flipping a flag back to a
previously-set value reuses a key, so the command stream carries no distinct
identity per occurrence even if a command log were added later.

### 7.6 The publisher may not be a person

`evidence_ref` and `published_by_ref` are both `NOT NULL`
(`models.py:303-305`). The admin path derives the actor as
`str(actor_id or f"customer:{before.id}")`
(`app/services/web_customer_actions.py:2630`) — when no staff actor is present
it fabricates a `customer:<uuid>` string, i.e. the customer appears to have
approved their own exemption. Rows whose `updated_by` is null or matches
`customer:<uuid>` get `basis_code="legacy_state_snapshot"` and
`published_by_ref="sub:system:legacy_backfill"`, and are flagged for operator
adjudication rather than presented as an operator declaration.

### 7.7 There is no exemption evidence to reference

Nigerian VAT exemption normally rests on a certificate or a statutory category.
Sub stores neither: `CustomerTaxPolicy` has no document reference, and no
attachment table is linked to it (`app/models/customer_tax_policy.py:11-53`).
`evidence_ref` therefore points at the Sub record itself and is explicitly a
pointer to an *unevidenced operator decision*. Section 1 gate 4 and 7.6 exist so
this is adjudicated before these classifications become authoritative, not
after.

## 8. What Sub stops owning at cutover, and what it keeps

### 8.1 Stops owning (C6, except where noted)

| Retired | Path | Ledger step |
|---|---|---|
| `TaxRate` model and table | `app/models/billing.py:2376-2396` | C6 |
| `financial.tax_configuration` service | `app/services/billing/tax.py:19-117` | C6 |
| `CustomerTaxPolicy.vat_exempt` as a *decision* | `app/models/customer_tax_policy.py:36-40` | C6 |
| the whole compatibility resolver | `app/services/billing_tax_resolution.py` (entire module) | C6 |
| its registry entry | `app/services/sot_registry/domains/financial_access/invoicing_tax.py:1182-1332` | C6 |
| `CatalogOffer.with_vat` / `vat_percent` as *readable* values | `app/models/catalog.py:566-567`; readers at `billing_tax_resolution.py:189-203`, `web_catalog_calculator.py:82-93,108-137,206-207`, `web_catalog_offers.py:188-189,242-243,306,327,676-677`, `templates/admin/catalog/offer_form.html:599-601,664`, `templates/admin/catalog/calculator.html:41` | C6 |
| those two fields in the live-immutability set | `app/services/catalog_billing_governance.py:52-53` | **C7** |
| those two columns | migration | after C7 |
| `Subscriber.tax_rate_id`, `Address.tax_rate_id` as inputs | `app/models/subscriber.py:340,721` | C6 |
| settings `billing.default_tax_rate_id`, `billing.default_tax_application` | `billing_tax_resolution.py:52-76` | C6 |
| the tenant-wide inclusive/exclusive toggle as a concept | see 6.8 | C6 |

### 8.2 Keeps owning

- **Billing facts and documents.** Invoices, credit notes, lines, amounts,
  `billing_line_key` identity (`app/models/billing.py:1117-1146`). Sub is the
  source of the facts tax determines against.
- **Issued-document tax snapshots as immutable evidence.**
  `invoice_lines.tax_rate_id` / `tax_application` and `invoices.tax_total` stay
  readable historical evidence. They are never recomputed, never rewritten, and
  after cutover are never *inputs* — only the historical record of what was
  charged. The `tax_rates` rows they reference must survive the retirement of
  the `TaxRate` *owner* for as long as any issued document points at them; the
  table becomes frozen reference data with no writer, and only drops when no
  live document references it.
- **The withholding evidence lifecycle.** `WithholdingTaxRecord`, its statuses
  and transitions (`app/services/tax_accounting.py:1-8,28-32`). Sub's own
  docstring already draws this boundary against ERP and it is unchanged here.
- **Customer, subscription, service-address and catalog identity**, including
  `supply_ref` and `place_ref` subjects.
- **The operator intake screen for exemption**, demoted to a thin adapter that
  publishes a `TaxSubjectClassification` through the tax package instead of
  writing a local boolean that a local resolver reads.
- **Entitlement, provisioning and access consequences** — untouched by tax.

## 9. Removing the clone-forcing guard (ledger C7)

The edit is the removal of `"vat_percent"` and `"with_vat"` from
`_OFFER_CRITICAL_FIELDS` at
`app/services/catalog_billing_governance.py:52-53`, which removes them from
`_OFFER_LIVE_IMMUTABLE_FIELDS` (`catalog_billing_governance.py:56`) and from the
live-mutation refusal at `catalog_billing_governance.py:200-210`.

**It belongs to ledger step C7 and to no earlier step.** All of the following
must be true first:

1. C5 is implemented: the outbox exists and emits facts; classifications are
   backfilled and adjudicated (sections 1, 6.3, 7.6, 7.7).
2. C6 shadow comparison over all three reader paths (2.2) plus the calculator
   surface shows zero unexplained drift for a named cohort.
3. C6 has switched recurring, prepaid and manual reads together — the ledger is
   explicit that they switch together, and a partial switch leaves two live tax
   owners.
4. `app/services/billing_tax_resolution.py` is deleted, taking with it the only
   money-path readers of the two fields
   (`billing_tax_resolution.py:79-96,189-203`).
5. `web_catalog_calculator.py` no longer reads them
   (`web_catalog_calculator.py:82-93,108-137,206-207`) — it is an operator
   decision surface, not a harmless projection.
6. The write path no longer accepts them: `app/schemas/catalog.py:324-325,364-365`
   and `app/services/web_catalog_offers.py:242-243,327` stop binding the form
   fields, and `templates/admin/catalog/offer_form.html:599-601,664` stops
   rendering them.

Only when the fields are unreadable and unwritable compatibility data does
removing them from the immutability set stop being a loosening of a money guard
and become the removal of a guard over dead data. The column drop is a separate,
later, proven migration — not part of C7.

Removing the guard while any reader survives would let an operator change the
VAT treatment of a live offer and silently re-rate existing subscriptions. That
is strictly worse than the clone problem the programme is solving.

## 10. Architecture guards this design owes (ledger H2)

Named here so C6/C7 cannot close without them:

- no tax rate, percent or treatment field on any catalog row;
- no reader of `CatalogOffer.with_vat` / `vat_percent` outside the frozen
  historical-evidence path;
- exactly one emitter of `TaxFact` in Sub, and no direct
  `determine_tax_set` call from an adapter;
- `TaxFact.party_category`, `supply_category` and `place_code` are `None` at
  every Sub call site (6.2, 6.3, section 4) — a sensitivity proof must show the
  guard fails when a non-null category is introduced;
- the outbox is the only writer of `tax_fact_exports`, and no Sub code
  recomputes a tax amount for an already-issued document.

## 11. Open items blocking C5 implementation

1. ERP's tax vocabulary and jurisdiction/tax-code identities are unpublished
   (section 1.1, 4.1).
2. `dotmac-tax` release evidence is outstanding (ledger B6-B8).
3. The `with_vat=false` offer population needs per-offer operator adjudication
   between the supply and party readings (6.3).
4. The discount apportionment rule and its rounding residual assignment need to
   be decided and written down (6.1).
5. The `occurred_on` conversion timezone must be declared (6.5).
6. Exemption evidence policy: Sub holds none, so what an operator must produce
   before a classification is treated as authoritative is undecided (7.7).
7. The pre-backfill blind window must be accepted explicitly as a limit on the
   C6 zero-drift gate (7.5).
