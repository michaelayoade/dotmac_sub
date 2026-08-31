# Receivable projection shadow (`receivable-shadow-01`)

Owner: `billing.receivable_projection`
(`app/services/billing/receivable_projection.py`).
Status: shadowing. **No authority moves in this slice, and none is proposed by
it.**

This document is the design of record for the Subscription → Billing →
Collections receivable projection: which rows it covers, how the cohort is
sealed, what provenance it stores, how a rebuild works, what parity it can and
cannot claim, and which commands an operator runs.

## 1. What this is, and what it is not

`BillingReceivableProjection` is a **rebuildable projection of facts other owners
already decided**. It is not a receivable, not an invoice, and no decision path
reads it. Every incumbent writer keeps its state:

| State | Owner | Only writer |
| --- | --- | --- |
| `invoices.status`, `balance_due`, `paid_at` | `financial.invoices` | `_recalculate_invoice_totals` (`app/services/billing/_common.py`) |
| Terminal void / write-off | `financial.invoices` | `Invoices.confirm_void` / `confirm_write_off` |
| Payment allocation and settlement | `financial.payments` | `app/services/billing/payments.py` |
| Settlement mirror (`payment_provider_events`) | `financial.payment_provider_events` | `_admit_event` (`app/services/payment_provider_events.py`) — the single `PaymentProviderEvent(...)` construction site |
| Transport receipt (`integration_inbox`) | `integration.inbox` | `receive_verified` — the single `IntegrationInbox(...)` construction site |
| ADR 0007 collections case | `collections.lifecycle` | `_advance` — the single `CollectionsCase(...)` construction site |
| Legacy dunning case | `financial.dunning` | `app/services/collections/_core.py` |
| Contract terms and `source_version` | `billing.contracts` | `app/services/billing/contracts.py` |

The projection reads these and writes only `billing_receivable_projections` and
`receivable_projection_runs`. **No collections case is created by anything in
this slice**, and `tests/architecture/test_receivable_projection_boundary.py`
enforces that statically, with a sensitivity proof so the guard cannot silently
stop matching.

## 2. Two things called a cohort

`app/shadow/cohort.py` declares a **module-adoption** cohort: twenty-five
packages and how far each has actually travelled. Every entry is `source_only`
with `authority_mode = none`.

`app/services/billing/receivable_cohort.py` declares a **data** cohort: which
rows are compared.

Neither implies the other. Recording a data cohort does not advance a module
one step along `ADOPTION_PROGRESSION`, and nothing here writes to that
manifest. Both declarations read the immutable Subscriptions release
coordinates from `app/module_release_contracts.py`, so production code does
not import the shadow package and the blocker cannot drift from the adoption
pin.

## 2a. Product projection and module input are deliberately distinct

`dotmac-collections` owns a **pure value object** called
`ReceivableObservationV1`: the peer input an assembly maps a Billing fact into,
carrying the already-funded collectible amount plus decision provenance and
nothing else.

`app/models/billing_receivable_projection.py` declares Sub's **persisted local
projection row** as `BillingReceivableProjection`. The different names state
the boundary even when both distributions are installed: the product row is a
rebuildable input that an assembly may map into the module value object. It is
not a second writer of the module contract, and neither package imports the
other.

## 3. The cohort definition

**Anchor: the incumbent `invoices` row.** Not the ADR 0007 obligation. The
reconciler observes what the incumbent receivable authority already decided;
the obligation, where one exists for the same subscription and period, is
carried as provenance and is the counterparty in the `obligations` parity
dimension.

**Candidate**: one `invoices` row with `is_active`, `created_at` inside the
half-open observation window `[window_start, window_end)`, and
`created_at <= cutoff_at`.

**Member**: a candidate that additionally satisfies all of:

1. `status` ∈ {`issued`, `partially_paid`, `overdue`, `paid`, `written_off`};
2. `issued_at` is non-null and inside the window;
3. at least one active `invoice_lines` row carries a `subscription_id`;
4. every such line names the *same* subscription;
5. that subscription resolves, and its `billing_mode` maps to a declared lane.

**Lane**: read from `Subscription.billing_mode`. `postpaid → postpaid_receivable`,
`prepaid → prepaid_consumption`.

### 3.1 Both collection modes, deliberately

The cohort spans postpaid **and** prepaid. Sub's two delinquency paths diverge
operationally — `financial.dunning` drives the postpaid `DunningWorkflow` while
`collections.prepaid_balance_sweep` owns a separate scheduled cohort scan with
its own timers, notices and plan — and they converge only at shared financial
access. A cohort covering postpaid alone would let a "Subscription → Billing →
Collections parity" claim rest on half the system.

The lane is an accounting observation carried through the projection. **Nothing
in this slice selects, triggers, or simulates a dunning path.**

### 3.2 Exhaustive classification

Every candidate lands in exactly one bucket, and the counts sum to the
candidate total. There is no residual bucket.

| Classification | Meaning |
| --- | --- |
| `covered` | Resolved, in the compared set |
| `unresolved` | Subscription, lane, status vocabulary, or issue instant did not resolve |
| `ambiguous` | Lines named more than one subscription |
| `unexpected_unlinked` | No active line carries a `subscription_id` |
| `duplicate` | Two candidates collapsed onto one receivable key |
| `excluded_by_status` | Declared exclusion: `draft` or `void` |
| `not_expressible` | Resolved, but no contract version **and** no obligation, so the ADR 0007 dimensions have no counterparty |

`ReceivableProjectionRun.unclassified_count` is a canary that must stay zero.

### 3.3 Sealing and reproducibility

Two digests answering two questions:

* **`definition_seal`** — sha256 over the declaration plus the three window
  instants. Reproducible from source code and the window alone, no database.
  Two runs claiming the same cohort applied the same rule iff their seals match.
* **`membership_digest`** — sha256 over the sorted, de-duplicated member keys.
  Requires the database at the same cutoff.

Same `definition_seal`, different `membership_digest` ⇒ **the source drifted**,
not the projection. That is the signal `repair-drift` exists to surface.

Naive datetimes are refused, never coerced: a cohort whose boundary depends on
the reader's timezone is not sealed.

## 4. The projection version, and why it is not `source_version`

The brief asked to replace a report-local `source_version=1` with a durable
monotonic receivable projection version. On inspection, the `source_version` in
this repository is **not report-local and is not a placeholder**:

* it is a column on `billing_contract_versions` and `billing_obligations`
  (`app/models/billing_contract.py`);
* it is written by `billing.contracts`
  (`app/services/billing/contracts.py`, including the
  `current.source_version + 1` supersession step) and threaded into obligations
  by `billing.obligations`;
* it is a component of `uq_billing_obligation_natural_identity`.

It answers **"which revision of the upstream contract source produced these
terms"**. Overloading it with a projection watermark would create a second
writer of an existing versioned field and would change obligation identity.

So this slice introduces a **separate** counter, `projection_version`, which
answers a different question: *"which revision of the observed incumbent state
does this projected row carry"*. `billing.contracts` keeps `source_version`;
the projection **reads** it into `contract_source_version` as provenance and
never writes it.

`projection_version` is allocated from the PostgreSQL sequence
`billing_receivable_projection_version_seq`, so it is monotonic across concurrent
workers, not merely within one process, and is usable as a watermark by an
incremental reader. The SQLite fast lane falls back to `max + 1`, which is
explicitly non-authoritative for concurrency.

## 5. The monotonic guard is structural

Three layers, each catching what the one below cannot:

1. **Plan** — the pre-loaded projection row's `source_observed_at` is compared
   to the freshly derived watermark, so a stale observation is classified before
   any statement is issued.
2. **Statement** — on PostgreSQL the upsert carries
   `ON CONFLICT (receivable_key) DO UPDATE ... WHERE
   excluded.source_observed_at > billing_receivable_projections.source_observed_at`,
   closing the window between the plan's read and its write.
3. **Schema** — `trg_billing_receivable_projections_monotonic` (migration
   `558_receivable_projection`, after
   `557_outbox_relay_prereq`) is a
   `BEFORE UPDATE` trigger that refuses any update which does not strictly
   advance `projection_version`, or which moves `source_observed_at` backwards.

(1) makes reconciliation converge. (3) makes the invariant *unrepresentable*: a
future writer that forgets (2) is refused by the database rather than quietly
overwriting a newer fact with an older one. That is why
`tests/integration/test_receivable_projection_monotonic.py` bypasses the
service and issues raw SQL — it asserts the database refuses the write, not
that the service declines to attempt it.

**`source_observed_at`** is the newest instant among the contributing source
rows: the invoice's `updated_at`, its active lines' `updated_at`, its active
allocations' `created_at`, and its credit-note applications' `updated_at`.
Staleness is judged on that, never on `projected_at` — a projection that
compares its own clock is measuring when it looked, not when the fact changed.

**Equal watermark, different fingerprint** fails closed: nothing is written and
the position is counted in `ambiguous_watermark_count`. Two facts at one instant
is not a tie to be broken by whichever query ran last.

## 6. Provenance and rebuild

Every input a rebuild needs is on the row: `cohort_definition_seal`,
`cohort_definition_version`, `projection_policy_version`, `invoice_id`,
`account_id`, `subscription_id`, `contract_version_id`,
`contract_source_version`, `obligation_id`, `invoice_line_ids_sha256`,
`allocation_ids_sha256`, and `input_row_fingerprint` over the ordered source
tuple.

**Rebuild** = delete the projected rows and replay the reconciler
(`run_kind=backfill`) over the same sealed window. Every
`input_row_fingerprint` must reproduce byte for byte.

`projection_version`, `projected_at` and `projected_by_run_id` are **excluded**
from that fingerprint by design: they change on every rebuild, and folding them
in would leave the projection unable to prove it had reproduced anything.

**Orphans are reported, never pruned.** A projected row whose invoice has left
the cohort is counted in `orphaned_count` and left alone. Pruning on a window
change would destroy the ability to audit a run against the evidence recorded
for it, and "the cohort changed" is not the same fact as "this observation was
wrong".

## 7. `observed_outstanding_amount` is an observation

It records what the incumbent already holds (`invoices.balance_due`), once, so
parity can compare it. It is **not** a third derivation for consumers to switch
to. Sub already has two independent derivations of outstanding —
`collections/postpaid_policy.py` and `collections/prepaid_policy.py` each
compute `gross - resolved` from the obligation — plus `financial.invoices`'
`balance_due`. A reader that needs a decision reads those owners.

`observed_settled_amount` is derived as `total - balance_due` from the
incumbent's own two numbers rather than re-summed from allocations, for exactly
the same reason: re-summing would make the projection a competing derivation of
the number it exists to observe.

## 8. Semantic parity

Seven dimensions, each evaluated and reported independently
(`app/services/billing/receivable_parity.py`, read-only):

| Dimension | Compares |
| --- | --- |
| `cadence` | `Subscription.billing_cycle` against the contract version's invoice interval, via a declared calendar-term equivalence (never a day count — ADR 0007 invariant 6) |
| `proration` | The projected proration policy against the contract version's |
| `obligations` | The covering `billing_obligations` row's gross against the invoice total, same currency only |
| `settlements` | The projected settled amount against `resolve_invoice_settlement_amounts` — a **read** of the incumbent owner, not a re-derivation |
| `receivable_amount` | The projected outstanding against `invoices.balance_due` |
| `due_date_provenance` | The observed `due_at` / `due_date_basis` / `basis_ref` / `policy_version` against `issued_at + BillingContractVersion.payment_terms_days` |
| `service_scope` | The projected service-scope fingerprint against a freshly derived one |

### 8.1 Three outcomes, never two

`matched`, `diverged`, `not_expressible`. The third is the point: folding "we
cannot compare this" into either of the first two makes a parity claim cover
less than it appears to. `NotExpressibleReason` is a closed enum, so the counts
can be grouped rather than accumulating two spellings of one reason.

### 8.2 The standing parity ceiling — Subscriptions pin

**Cadence and treatment parity cannot be claimed for complimentary or sponsored
subscriptions on this tree.**

Sub carries its own authoritative non-standard billing treatment
(`subscription_billing_arrangements`, owned by
`financial.subscription_billing_treatments`). The pinned Subscriptions contract
— `dotmac-subscriptions`, version and revision read live from
`app/module_release_contracts.py` — supplies the corresponding contract, but
this additive schema-composition phase admits no application import, runtime
reader, backfill, or mapping to that contract.

Synthesising a mapping before the runtime contract is admitted would make Sub
a second interpreter of data it does not read. So such positions are counted
`not_expressible` on the cadence dimension, with the pin coordinates attached
to the blocker recorded on the run row — never `matched` because nothing
contradicted them.

Blocker code:
`subscriptions-billing-treatment-contract-not-runtime-adopted`.

### 8.3 Due-date authority gate

`BillingContractVersion.payment_terms_days` exists and is populated, and
nothing computes `invoices.due_at` from it: the live issuance sites each
resolve their own day count through `resolve_payment_due_days`
(`app/services/billing_settings.py`), whose precedence is
`Subscriber.payment_due_days` → `DomainSetting(billing, payment_due_days)` →
two legacy keys → caller default. A divergence here is therefore expected on
the current tree. It is reported, counted, and **left alone** — repairing it
would move authority.

The real resolver is intentionally gated, not guessed in this shadow slice:

1. `billing.contracts` must first move from `shadowing` to an approved
   authoritative migration state; shadow `BillingContractVersion` rows cannot
   drive collectible invoice dates.
2. `financial.invoices` must own a typed due-date-resolution contract in
   `app/services/billing/invoices.py`, consuming
   `BillingContracts.effective_version_at(...)` and recording the exact
   contract-version reference and policy version on `InvoiceIssuanceInput`.
3. `app/services/billing_automation.py`,
   `app/services/billing/invoices.py`, and subscription-backed issuance in
   `app/services/crm_api.py` must delegate to that resolver. Multi-subscription
   invoice grouping must fail closed unless every effective version agrees on
   the applicable payment terms; selecting the first subscription would create
   an unowned precedence rule.
4. The registry cutover, behavior tests, parity expectation, and removal of the
   legacy `resolve_payment_due_days` subscription path land together.

`tests/architecture/test_receivable_projection_boundary.py` enforces the
current half of this gate: while contract rows are shadow evidence, no native
invoice writer may read `payment_terms_days` or call the effective-version
resolver. That test must be replaced by positive resolver and aggregation
tests in the authority-cutover change, not deleted in isolation.

### 8.4 The sealed read-only readiness verdict

`assess_receivable_cutover_readiness` answers the stronger question that
`parity --strict` intentionally does not: *is this evidence complete enough to
be eligible for an authority review?* It binds the cohort definition seal,
membership digest and parity report fingerprint into one readiness fingerprint
and fails closed when any of these is true:

1. the report fingerprint no longer reproduces or the cohort, projection,
   position and dimension counts do not account for one another;
2. the compared cohort is empty (an empty query cannot prove parity);
3. a candidate remains unresolved, ambiguous, unexpectedly unlinked,
   duplicated or not expressible;
4. a read-only projection plan would still insert or update a row, refuses a
   watermark, or sees an orphan;
5. any semantic dimension diverges or cannot be expressed; or
6. the cohort carries a standing pinned contract blocker.

The plan and comparison are separate reads. Their count invariants therefore
also catch a projection row moving between those reads and fail closed instead
of treating a mixed statement snapshot as sealed evidence.

The verdict is a resolver owned by `billing.receivable_projection`; it writes
nothing and has no `--apply` form. A passing verdict is not an authority flag,
a migration-state change, a writer switch, permission to retire a fallback, or
permission to deploy. It makes the sealed evidence eligible for the separately
authorised review described by ADR 0007. On this tree it truthfully remains
blocked by the Subscriptions treatment seam and incomplete obligation coverage.

Commercial Agreements and the independently deployed Integrator remain external
owners and are not imported or pinned by this product query. Vendor legal
agreement lifecycle is not a Sub receivable input, and connector publication is
not settlement evidence. Adding either as a runtime dependency would blur the
application boundary without closing one readiness condition.

## 9. Persistence shape, and the absent tenant column

`billing_receivable_projections` and `receivable_projection_runs` carry **no
`tenant_id` and no RLS policy**, deliberately. Every authoritative input —
`invoices`, `invoice_lines`, `payment_allocations`, `subscriptions`,
`billing_obligations` — is tenant-free. Sub is a single-operator data plane
whose tenancy is the ADR-0009 operator bridge, not a row-level column on
financial tables.

A `tenant_id` here would have no authoritative source to fill it and would
produce an RLS policy that is decorative rather than isolating. The absence is
asserted structurally (both in the architecture test and against the migrated
PostgreSQL catalog) so a later editor cannot half-add one. **If those inputs
ever become tenant-scoped, this table takes `tenant_id NOT NULL`, composite
uniques and RLS in one migration** — the standard applies, its precondition
does not hold today.

## 10. Operator commands

`scripts/billing/receivable_projection.py`. **Dry run is the only default, and
there is no way to spell it wrong**: every writing subcommand takes `--apply`
and there is deliberately no `--dry-run` flag. A flag that must be *present* to
be safe is one typo or one edited runbook line away from an unintended write.

A dry run does not enter `execute_owner_command` at all. It builds the same
plan the apply path builds, returns the same typed result, and persists
nothing — no projected row, no run row. That is stronger than "opened a
transaction and rolled it back": there is no write to forget to undo.

```sh
# Print the sealed cohort rule. Never writes, with or without --apply.
poetry run python -m scripts.billing.receivable_projection cohort \
  --window-start 2026-07-01T00:00:00+00:00 \
  --window-end   2026-08-01T00:00:00+00:00 \
  --cutoff       2026-08-25T00:00:00+00:00

# First population. Dry run unless --apply.
poetry run python -m scripts.billing.receivable_projection backfill \
  --window-start ... --window-end ... --cutoff ... \
  --code-version <git sha> --schema-version <alembic revision> \
  --idempotency-key <key> [--apply]

# Convergence pass, drift detection and repair, parity report.
poetry run python -m scripts.billing.receivable_projection reconcile    [same args]
poetry run python -m scripts.billing.receivable_projection repair-drift [same args]
poetry run python -m scripts.billing.receivable_projection parity       [same args]
poetry run python -m scripts.billing.receivable_projection readiness    [same args]
```

`--strict` exits non-zero on drift (`missing`, `stale_skipped`,
`ambiguous_watermark`, `orphaned`) or on a parity divergence, so the same
command is usable as a gate. A `not_expressible` count is **not** a strict
failure: it is a recorded, pinned limit, not a regression.

`parity --apply` records the report as durable run evidence and still changes no
projected row.

`readiness` is always read-only and exits non-zero whenever its typed verdict is
blocked. It first reproduces the report seal and aggregate accounting. Unlike
`--strict`, it also treats every `not_expressible` count and every standing
contract blocker as a failure, because those are lawful shadow evidence but
incomplete authority evidence.

## 11. Run evidence

`receivable_projection_runs` is shaped to ADR 0007's cutover-evidence standard:
schema and policy version, cutoff and observation window, exhaustive cohort
classification, source and result fingerprints, per-currency money totals
(strings, never floats — ADR 0007 invariant 13 forbids nominal cross-currency
comparison), the blocker categories with pin coordinates, and the exact code and
schema versions. A dry run persists none of it.

## 12. Validation

```sh
poetry run ruff check app tests scripts alembic
poetry run ruff format --check app tests scripts alembic
poetry run mypy app --ignore-missing-imports --no-incremental
poetry run lint-imports
poetry run bandit -r app -c pyproject.toml -q
make test-architecture
make test
make test-integration   # required: the PostgreSQL trigger, sequence, RLS-absence
                        # and concurrency coverage lives here and cannot run on
                        # the SQLite fast lane
```
