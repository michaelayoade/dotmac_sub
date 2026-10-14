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
function definition with its lexical qualified name and occurrence. Current
invoice constructors and proforma conversion delegate through the incumbent
`financial.invoices.next_invoice_number` owner. That owner remains the only
place where invoice sequence policy is applied. The future inventory above
identifies the callers that must be reviewed for a one-writer switch; it does
not activate them. The installation helper remains pinned to its canonical
top-level definition and must occur exactly once across the whole service tree.
Duplicate top-level definitions, including non-allocating duplicates, and
class or nested substitutes cannot satisfy its identity.

The census owner changes none of the callers and leaves supplied labels and
credit-note lifecycle semantics unchanged. Existing drift requires review by
`financial.invoices` or `financial.credit_notes`; the census cannot repair it.

Unit canaries cover grammar, padding, counters, duplicates, configuration,
completeness and digests. Architecture canaries forbid writer imports/calls and
transaction completion; PostgreSQL canaries exercise the real migrated schema,
read-only denial, repeated snapshots, concurrent committed changes, cache
restoration and absence of new module/idempotency state. Hosted execution is
required before treating these unexecuted canaries as acceptance evidence.


## Deferred invoice-numbering activation — 2026-10-14

Invoice creation and proforma conversion continue using the existing
`public.document_sequences` invoice series and its configured prefix and
padding. This preserves the current customer-visible format and does not
require a Numbering series or counter to exist. The separate census above is
read-only; this change neither seeds Numbering state nor routes runtime writes
to it.

Activation remains deferred until the complete-population census and historical
formatting replay are reviewed, an invoice-number grammar and rollback inverse
are approved, all incumbent writers can be drained, and a single-writer cutover
has an operator runbook. A proposed target prefix changes the visible format
of newly issued invoices and therefore requires an explicit business decision;
it must not be inferred from the readiness report. Any future activation must
update this design, the typed service registry, migration and repair contracts,
architecture guards, and PostgreSQL integration evidence together.
