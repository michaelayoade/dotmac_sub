# Billing numbering readiness

`financial.billing_numbering_readiness` owns a read-only complete-population
census and a proposed seed for the operator tenant's `invoice_number` and
`credit_note_number` series. The released `dotmac-numbering==0.1.0a2` storage
composition is prerequisite. This slice does not activate a series, seed a
counter, allocate a number, create a receipt, repair a document or change a
runtime writer. Incumbent financial owners remain authoritative.

Run the standalone adapter with an explicit document reference date:

```sh
python -m scripts.migration.billing_numbering_readiness --reference-date 2026-10-13
```

The adapter uses `read_only_snapshot_session`: PostgreSQL repeatable read,
read-only, operator tenant scope. It temporarily disables only its process-local
kernel settings cache store through the public kernel API, restores it in
`finally`, and does no Redis invalidation. The canonical settings resolver then
reads within the snapshot. The owner never changes a global cache. Calling the
owner inside another process requires a caller-provided fresh, stable snapshot
and canonical-settings freshness. Cached settings or a moving READ COMMITTED
population cannot support seed approval.

Exit 0 means an admissible proposal, 1 means a complete census with blockers,
and 2 means incomplete/unavailable evidence. A successful report is a proposal,
not authorization to seed or cut over. Database/query failures propagate and
must be treated as unavailable evidence; no partial report is admitted.

The configuration preserves the effective canonical prefix verbatim, including
Unicode and trailing punctuation. Separator and suffix are empty, year/month
are disabled, reset is `never` with public `period_for` result `*`, and
`min_digits=max(1, legacy_padding)`. This is byte-equivalent for positive legacy
values, including zero padding. Prefix length above 32, padding outside 0..18,
and non-positive or exhausted counters/starts are refused. Values must remain
strictly below the published module maximum of 10^15. Existing module
configuration must match every proposed field; unexpected periods and orphan
module counters block admission. Every module allocation receipt is independently
counted and scanned in UUID order; its ID, period, value, rendered label and
reference date are bound to the digest. Missing configuration or period counter,
a counter at/below any receipt value, a non-never period, invalid value, or a
receipt whose public formatting disagrees blocks admission rather than silently
repairing history. Receipt scanned count must equal its database COUNT.
No existing counter may rewind.

Every Invoice and CreditNote row participates, including drafts, inactive,
void and closed documents. The query fetches only UUID and raw number, ordered
by UUID in batches, plus an independent database COUNT. Matching requires the
exact prefix, a nonempty ASCII positive integer remainder, and exact public
`format_number` round-trip using an unattached transient released NumberSeries.
Null, empty, matching and other counts must sum to scanned and database total.
Ordered IDs must be unique. There is no sampling or lifecycle filter. Other
grammars and historical duplicate labels are reported without data repair;
they cannot equal a canonical candidate proven strictly above the matching MAX.
Duplicate occurrence tracking consumes memory proportional to distinct labels;
operators must budget the full population rather than introduce sampling.

The proposal is the maximum of stored legacy `DocumentSequence.next_value`
(or configured start when absent), matching maximum plus one (or configured
start when absent), and existing module next values. An invalid, exhausted or
incompatible state has no proposed next value or preview. Only the released
public formatter and period functions are used; configuration, allocation,
advance and incumbent writer helpers are forbidden in the owner and adapter.

The versioned SHA-256 evidence binds operator scope, reference date,
configuration, legacy counter, every existing module configuration/counter and the complete allocation receipt stream,
completeness counts and the ordered `(kind, document UUID, raw number)` stream.
Observation time is metadata and excluded from the digest. Raw population UUIDs
and numbers are not printed. Changed historical labels, IDs, settings or
counters invalidate the proof. A later seed owner must take a fresh census under
incumbent-writer exclusion, compare the approved digest and advance only; this
report alone does not exclude writes after its snapshot ends.

The runtime caller inventory for the future one-writer switch is:

- `Invoices.stage_admin_draft`, `Invoices.stage_system_invoice`,
  `Invoices.create`, `Invoices.create_for_subscription`.
- `billing_automation.run_invoice_cycle`,
  `billing_automation.generate_prorated_invoice`.
- The unique top-level `create_installation_invoice` helper used by
  [`sales_orders.ensure_installation_invoice_for_sales_order`](../../app/services/sales_orders.py),
  following the existing [Sales-to-Service owner chain](SALES_TO_SERVICE_LIFECYCLE_SOT.md).
- `invoice_draft_authoring` proforma conversion.
- `CreditNotes.create`, `CreditNotes.issue_with_evidence`.

The architecture audit scans every service Python file and records every
function definition with its lexical qualified name and occurrence. In the
isolated allocation candidate it pins four invoice participant calls (the
three constructors and proforma conversion), two incumbent credit-note calls,
and four upstream delegates (subscription, scheduled cycle, proration and CRM
installation). The installation helper is pinned to the canonical top-level
`crm_api.create_installation_invoice` and must occur exactly once across the
whole service tree. Duplicate top-level definitions, including non-allocating
duplicates, and class or nested substitutes cannot satisfy its identity.

Stable document UUID, explicit authoritative reference date and caller-owned
transaction are implemented in that candidate, described below. The census
owner itself still changes none of the callers and leaves supplied labels and
credit-note lifecycle semantics unchanged. Existing drift requires review by
`financial.invoices` or `financial.credit_notes`; the census cannot repair it.

Unit canaries cover grammar, padding, counters, duplicates, configuration,
completeness and digests. Architecture canaries forbid writer imports/calls and
transaction completion; PostgreSQL canaries exercise the real migrated schema,
read-only denial, repeated snapshots, concurrent committed changes, cache
restoration and absence of new module/idempotency state. Hosted execution is
required before treating these unexecuted canaries as acceptance evidence.


## Isolated invoice allocation candidate — 2026-10-14

The read-only census owner above is unchanged. A separate, unmerged source
candidate routes all invoice construction and proforma conversion through
`financial.invoices.next_invoice_number`, which calls the released
`dotmac-numbering==0.1.0a2` allocator in the caller's transaction. It has no
runtime selector, incumbent fallback, series configuration or counter seed.
Credit-note allocation remains a distinct incumbent series.

The invoice owner captures its UUID and creation instant before allocation.
The reference date is the supplied issue instant, otherwise the captured,
persisted creation instant, normalized to UTC. It is not the service-period
start or a retry-time clock. `create_with_lines` carries the original issue
date separately before making its draft payload. Existing supplied labels and
adoption of existing documents allocate nothing. Initial and proforma
conversion requests have distinct identities; conversion additionally binds
its existing normalized command identity so an edited PF label cannot replay
an unrelated initial allocation.

This candidate is **not deployable or merge-ready**. A configured series alone
is not evidence that its counter was safely seeded. The participant requires
compatible configuration and an existing `*` counter, but those checks do not
authorize activation or exclude an older worker running incumbent code.

Before activation, exclude and drain every incumbent invoice writer, capture
and approve the complete census, perform pure historical formatting replay,
configure the reviewed series and advance its counter with independently
reviewed evidence, then prove read-only preview parity. The released
`advance_to_at_least(proven_minimum=X)` makes the next value `X + 1`; a proposed
next value must never be passed as the proven minimum without that conversion.
No allocation is spent to obtain preview evidence, and no dual allocation is
permitted. Legacy numbering configuration controls also need a reviewed
retirement or module-authoring path before deployment; the compatibility
check is not a second configuration writer.

The fleet ruling `numbering-cutover-shadow-allocation-is-incoherent` requires
a sealed one-writer flip with a stated inverse and identifies disjoint grammar
as the basis for identifying module-issued numbers on rollback. The census
proposal above preserves legacy grammar. That unresolved inverse conflict is
an activation blocker: this source candidate does not approve a grammar change
or a receipt-based exception. Before activation, abandoning the branch is the
inverse; after activation, incumbent writers cannot resume until the approved
counter-reconciliation procedure has completed.
