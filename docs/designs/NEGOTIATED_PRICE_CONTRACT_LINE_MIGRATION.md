# Negotiated price contract-line migration

Status: implementation inventory; not production cutover evidence
Source revision: `0324d2dcf91e2be038459e41aa013125128e9d43`
Target contract: `dotmac-subscriptions 0.1.0a2`, released as
`dotmac-subscriptions-v0.1.0a2`; merged to Starter `main` in PR #386 as
`473b473f94f194cbd768b5e110a4dc3f58d97cb7`

## Decision

A catalog offer identifies the product being sold. A subscription contract
line identifies the price accepted by one customer. A customer name, negotiated
amount, tax status, complimentary reason, location, or technical characteristic
must not create a new offer row.

The actual negotiated amount is a strictly positive `unit_price` on the
effective `dotmac-subscriptions` contract line. An offer version uses
`pricing_mode=contract_price` when the amount exists only on the agreement and
may therefore have no catalog price. A genuine public or internal rate-card
price may still be published as reference evidence; no fake reference price is
created to satisfy the catalog.

Discounts are not silently folded into a new offer price. Existing discounts
must either be preserved as separately evidenced commercial adjustments or be
adjudicated into the accepted contract-line amount, with the original source
and effective interval retained. Complimentary and sponsored service are never
represented by a zero contract line; they are positive-price lines resolved by
bounded non-cash grants.

## Evidence available at the pinned source

`docs/PLAN_FAMILY_ARCHITECTURE.md` records four customer-named offers from the
then-confirmed production inventory:

- `STM-1 Fiber (Norrenberger)`
- `200 Mbps Fiber mr richard`
- `700 Mbps Dedicated AScomnet`
- `Deen Global Innovation 600Mbps`

It also records 41 dedicated offers. This is historical checked-in evidence,
not a claim about the live database on 2026-08-23. No production host was named
for this work, so no production query was run. A fresh, read-only inventory from
an explicitly named production target remains a cutover gate.

Migration `489_unique_sellable_offer_name` records another distinct class: a
zero-price legacy `25 Mbps Fiber` row with two unbilled subscriptions. Those
rows belong to the complimentary/sponsored adjudication and grant migration,
not to negotiated-price normalization.

## Current owners and paths that must move together

### Source and mutation paths

| Current path | Current behavior | Required target |
|---|---|---|
| `app/models/catalog.py` (`Subscription.unit_price`) | Mutable subscription-level amount; nullable and not versioned | Read-only compatibility projection, then removal after contract-line cutover |
| `app/schemas/catalog.py` (`SubscriptionCreate`, `SubscriptionUpdate`) | Allows callers to submit the mutable amount directly | Typed contract-version command with actor, reason, effective interval and idempotency evidence |
| `app/services/catalog/subscriptions.py` create | Snapshots the active offer price into `Subscription.unit_price` | Create/publish the corresponding positive contract line |
| `app/services/catalog/subscriptions.py` offer change | Re-snapshots the new offer price unless a caller supplies an override | Plan-change owner versions the contract and line at the effective boundary |
| `app/services/web_catalog_subscriptions.py` | Admin form accepts and displays a direct subscription amount | Thin adapter submits a typed contract-version command and reads a contract-line projection |
| `app/services/crm_api.py` | CRM sale accepts an untyped price override and passes it into subscription creation | Typed sales-acceptance adapter supplies exact price/currency/cadence/source evidence to the contract owner |
| `app/services/web_provisioning_bulk_activate.py` | Constructs `Subscription` directly and snapshots the offer amount | Bulk activation calls the same subscription/contract lifecycle owner; no direct ORM price writer |
| `app/services/prepaid_renewal_terms_backfill.py` | Repairs and rewrites `Subscription.unit_price` from imported/contracted evidence | One-time input to the module backfill; never a post-cutover writer |
| subscription import paths (`app/schemas/imports.py`, `app/services/financial_imports.py` and migration importers) | Treat imported zero/missing/per-service price as a subscription field or silently default it from the offer | Classify as positive negotiated evidence, missing evidence, or zero-price grant adjudication; write through contract owner only |
| sales and fulfilment contract snapshots | Carry accepted `unit_price` into Sub's local billing contract | Adapt the same accepted amount into the module contract line, preserving source id/version |

New product writes are dual-written only during the bounded shadow phase. The
module write is the proposed authority; the compatibility `Subscription` write
exists solely so unchanged legacy readers can be compared. The cutover seals
the old writer in the same release that moves the final reader.

### Money and lifecycle decision readers

| Reader | Existing input | Shadow assertion before switch |
|---|---|---|
| `app/services/billing_automation.py` | Positive `Subscription.unit_price` overrides catalog and then receives any active discount | Exact pre-tax recurring amount, currency, cadence, coverage, proration and adjustment evidence match module rating |
| `app/services/prepaid_service_renewals.py` | Requires positive `Subscription.unit_price`; catalog supplies cadence/currency metadata | Exact renewal threshold and charge match the effective contract line and module cadence |
| `app/services/catalog/subscriptions.py` plan-change proration | Old effective override versus new catalog price | Exact old/new line identities, remaining coverage, credit, charge and net amount match |
| `app/services/prepaid_draft_reconciliation.py` | Compares draft charges to the subscription amount | Expected line, period and amount match module occurrence output |
| `app/services/prepaid_recovery_billing.py` | Builds recovery subtotal from `Subscription.unit_price` | Recovery selects the effective historical contract version and produces the same bounded amount |
| `app/services/billing/contracts.py`, `rating.py`, `obligations.py` | Local shadow contract/version/line and occurrence owners | Adopt module output, then retire local generic owners without retaining a fallback calculator |
| recurring add-on contract paths | Version positive add-on prices beside base service | Preserve independent line identity, amount, quantity and effective boundary |

### Reporting and presentation readers

The following are not permitted to remain as apparently harmless projections;
they influence revenue or operator decisions and therefore switch in the same
authority programme:

- `app/services/billing/reporting.py` MRR, ARPU, planned income and recurring
  revenue;
- `app/services/mrr_snapshot.py` and `app/services/web_reports_extended.py`;
- `app/services/customer_timeline.py`, `app/services/crm_api.py`,
  `app/services/subscriber_summary.py` and `app/services/web_subscriber_details.py`;
- `app/services/billing/shadow_verification.py`, which changes from comparing
  `Subscription.unit_price` to Sub-local contract rows to comparing the frozen
  legacy source snapshot with module contract lines and occurrences.

Invoice and credit-note line amounts remain immutable historical document
evidence. They are comparison outputs, never rewritten during this migration.

`web_reports_extended.get_custom_pricing_data` is not an inventory owner: its
comment says it finds prices that differ from the offer, but the query selects
every active subscription whose price is non-null and performs no comparison.
It must not be used as migration evidence; the typed exhaustive inventory below
replaces it.

## Inventory output and adjudication classes

The production inventory command must emit a stable, reviewable row per current
or historical subscription with at least:

- tenant, subscriber, subscription, offer and offer-version identifiers;
- offer code/name, service type, dedicated/shared classification and sellable
  state;
- catalog recurring prices, currencies, billing periods and effective dates;
- `Subscription.unit_price`, discount type/value/window and importer source;
- effective local billing-contract version/line, authority and source evidence;
- issued invoice-line price snapshots for the recent comparison window;
- current billing treatment or historical zero-price marker;
- proposed shared offer/specification mapping and module contract-line key; and
- one deterministic classification plus explanation.

The exhaustive classifications are:

1. `catalog_price_equal`: positive subscription amount equals one unambiguous
   effective catalog price;
2. `negotiated_positive`: positive amount differs from the shared offer's
   genuine reference rate or the offer is contract-priced;
3. `discount_separate`: a positive base price plus separately evidenced active
   commercial adjustment;
4. `historical_version`: an issued period correctly resolves to an older price
   or contract version;
5. `complimentary_or_sponsored`: zero/no-cash service requiring an evidenced
   positive reference/accepted price plus a non-cash grant;
6. `missing_price_evidence`: no positive amount can be established;
7. `ambiguous_price_evidence`: two or more plausible amounts, currencies,
   periods or sources disagree; and
8. `invalid_currency_or_period`: the amount cannot be composed into one exact
   contract line without operator correction.

Classes 6--8 block automatic backfill. Class 5 moves only through the grant
adjudication. An offer name is never used as an identity or automatic merge
key.

## Backfill contract

For every automatically resolvable subscription, the migration must:

1. publish or resolve one shared immutable module offer version linked to the
   canonical product/service specification;
2. record one module subscription contract version with a positive recurring
   line, exact currency/minor units, cadence, source id/version, actor, reason,
   command id, correlation id and deterministic idempotency key;
3. preserve the source effective interval and stable contract-line lineage;
4. store an assembly-owned mapping from legacy subscription/offer/version ids
   to module identities—never add a product foreign key inside the independent
   module;
5. fingerprint the source snapshot and proposed output so replays return the
   same identities and changed evidence conflicts; and
6. leave the module row non-authoritative until the cohort's shadow gate is
   approved.

Backfill never updates issued invoices, fabricates a catalog price, converts a
zero line to a paid customer balance, or merges two products merely because
their names or speeds look alike.

## Shadow and cutover gates

Sub's existing `billing.shadow_verification` evidence is the mandatory starting
point. Extend it to compare, per subscription and period:

- effective offer and contract version identities;
- contract-line key, charge model, quantity, currency and exact unit price;
- service/invoice cadence, collection timing and coverage interval;
- pre-tax amount, proration factor and rating-policy fingerprint;
- discount/adjustment evidence rather than only the net number;
- postpaid invoice draft, prepaid renewal, recovery and plan-change outputs;
- MRR, ARPU, planned income and recurring-revenue totals by currency; and
- missing, duplicate, gap, overlap, ambiguity, replay-conflict and unexpected
  legacy-write counts.

Cut over one named cohort only when it has zero unexplained variances, every
row is classified, all module outputs are receipted, an operator has approved
the fingerprinted run, and rollback means returning to the sealed pre-cutover
revision—not keeping two live price owners. After the last cohort:

1. all creation/import/plan-change writes call the module contract owner;
2. billing, prepaid, proration, recovery and reporting read the module line or
   its explicitly rebuildable projection;
3. direct mutation of `Subscription.unit_price` is database-refused;
4. customer-named offers created only for price are withdrawn from selection
   and mapped to the shared offer while historical invoice snapshots remain
   reachable;
5. the Sub-local generic contract/version/line writers and fallback calculators
   are deleted; and
6. architecture ratchets reject new price reads/writes outside the contract-line
   owner and demonstrate sensitivity in both directions.

## The read-only inventory command

`scripts/negotiated_price_offer_inventory.py` builds the inventory described
above. It has been written and unit-tested against its SQL construction; it has
**not** been run against any database, and this document records no inventory
result.

Three properties are structural rather than conventional:

- it takes its target from `--database-url` or `SUB_INVENTORY_DATABASE_URL`, has
  no default, never inherits the ambient application `DATABASE_URL`, and refuses
  to run when no target is named;
- every statement it can build is compiled and asserted to begin with `SELECT`
  before execution, and the session is pinned through `app.db`'s single
  read-only seam (`READ_ONLY_SNAPSHOT_OPTIONS`) to one `REPEATABLE READ, READ
  ONLY` snapshot;
- it reports evidence and signals only. The eight adjudication classes above are
  decisions owned by this migration and adjudicated by an operator; the command
  does not assign them. A "customer-like name" is reported as two separate
  signals — name tokens outside the product vocabulary, and name tokens that
  also occur in a stored customer name — each carrying the tokens that produced
  it, so a reviewer can see why a row surfaced. An offer name is never used as
  an identity or a merge key.

Contract tests: `tests/test_negotiated_price_offer_inventory.py`.

## Outstanding evidence before F1 can close

- Michael must explicitly name the production target before a read-only live
  inventory is run.
- The `dotmac-subscriptions 0.1.0a2` candidate must have exact Observer proof,
  registry installation evidence and a peeled release tag before Sub pins it.
- Every live dedicated/customer-named row and its subscriptions must be reviewed
  against the classifications above; the checked-in four-name/41-row snapshot
  is not sufficient current evidence.
- Currency, cadence and active-discount disagreements require named operator
  adjudication; absence of evidence is never converted to a guessed amount.
