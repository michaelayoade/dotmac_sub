# Sale → Money Handoff

**doc_kind:** design
**status:** proposed — shadow phase implemented, cutover not approved
**authority:** none. `financial.*` owns money today and after; this document
proposes how `sales.orders` stops keeping a second copy of it.

**Addresses:** open finding 5 of `SOT_PIPELINE_CLASSIFICATION.md`. That finding
remains open; implementing the shadow phase does not close it.

## The problem

`SalesOrder` stores `amount_paid`, `balance_due`, `payment_status` and
`paid_at`. These are Money-pipeline facts held as Sale-pipeline columns. They
are derived by `_apply_payment_fields` — plain assignment — rather than by the
invoice state machine (`ALLOWED_INVOICE_TRANSITIONS`), and nothing reconciles
them against the ledger.

One duplicated boundary has produced four separate money defects:

- a waiver silently revoked by a totals recalculation;
- a line discount restored to gross price by an unrelated edit;
- `amount_paid` inferred from `total` and posted to the ledger unevidenced;
- a second deposit overwriting the first rather than accumulating.

Each was fixed individually. None would have existed had the boundary been a
read.

**Sale owns the price. Money owns the money.** `subtotal`, `tax_total`, `total`
and line discounts are the commercial agreement and stay with the Sale.
`amount_paid`, `balance_due`, `payment_status` and `paid_at` are ledger facts.

## The boundary has no structure to contract over

There is **no foreign key** from `Invoice` or `Payment` to `SalesOrder`. Every
link is a metadata string:

| Artifact | Link |
| --- | --- |
| Installation invoice | `Project.metadata_["selfcare_installation_invoice_id"]`, reached from the order's Project |
| Subscription invoice | `SalesOrderLine.metadata_["selfcare_subscription_invoice_id"]` |
| Payment | `Payment.external_id == "crm:sales_order:{id}:payment"` |

`SALES_TO_SERVICE_LIFECYCLE_SOT.md` states the chain uses structural foreign
keys and that metadata identifiers are provenance rather than canonical joins.
**That is not true of this boundary.** It is also the reason the handoff was
never contracted: there was nothing structural to contract over.

## Settlement is allocation, not payment origin

An order-originated payment carries
`external_id = "crm:sales_order:{id}:payment"`. That proves **origin, not
application**. `_record_sales_order_payment` deliberately charges the *account*
rather than one invoice, and the ledger auto-allocates across whatever is open —
so an order's payment may have settled a completely different obligation.

**A direct `Payment.sales_order_id` must therefore never become the settlement
contract.** It would record where money came from, not where it landed.
Settlement is read through `PaymentAllocation` against the order's own invoices;
originating payments are carried separately, as provenance only.

## The obligation → document → application chain

Foreign keys alone do not achieve this. The structural target is:

```text
finite SalesOrder billing obligation
  → structurally linked Invoice / InvoiceLine
  → PaymentAllocation or credit application
  → Payment / settlement
  → refunds, reversals, credit notes and waivers
```

**An invoice-header foreign key is insufficient** where one invoice combines
several sources. The relationship belongs at line or obligation level, with
defined partial-allocation semantics — otherwise a recurring invoice that
descends from this sale's subscription would inflate the original sale merely by
being a descendant.

The next slice is therefore not "add foreign keys". It is to establish the
finite obligation, the document that expresses it, and the application that
settles it.

## Migration

| Phase | State |
| --- | --- |
| **Old owner** | `sales.orders`, storing derived money columns | 
| **New owner** | `financial.invoices` / `financial.payments`, read through `sales_billing_position` |
| **Shadow** | both computed; disagreement reported, never auto-corrected — **implemented** |
| **Structural join** | real foreign keys replace the metadata strings — *not started* |
| **Cutover gate** | drift understood and at zero across the active cohort, with finance sign-off |
| **Retirement** | columns dropped, `_apply_payment_fields` deleted |

### Shadow phase (implemented)

`app/services/sales_billing_position.py` resolves a sales order's position from
the ledger and compares it with the stored columns. It writes no sales or
billing state.

**Every in-scope order lands in exactly one bucket.** The scan asserts the
bucket counts sum to the scanned total, so an unclassified order fails the run
rather than silently shrinking the denominator and making a dirty cohort look
clean.

| Bucket | Meaning | Blocks cutover |
| --- | --- | --- |
| `waived_excluded` | settled by decision, canonical waiver evidence present | no |
| `waived_evidence_missing` | marked waived without the owner's evidence | **yes** |
| `unlinked_expected` | no artifacts and none due yet (draft, or nothing to bill) | no |
| `unlinked_unexpected` | no artifacts although the order should have them | **yes** |
| `unresolved_invalid` | metadata identifier is not a well-formed id | **yes** |
| `unresolved_missing` | well-formed id pointing at no live invoice | **yes** |
| `unresolved_ambiguous` | artifact reachable from more than one sales order | **yes** |
| `agreeing` | linked, resolvable, stored columns match the ledger | no |
| `drifting` | linked, resolvable, stored columns disagree | **yes** |

Unsafe joins are classified *before* comparison: a comparison across a join we
do not trust is not evidence of anything. Ambiguity is real — the installation
invoice path deliberately reuses an invoice across projects sharing a sales
order or quote, and an invoice that cannot be attributed to one obligation
cannot carry the boundary.

### Durable evidence

Warning logs are alerts, not cutover evidence — they rotate, and a consecutive
clean window cannot be proven from them. Each scan appends an immutable
`sales_billing_shadow_runs` row carrying the contract version, a cohort
fingerprint (a stable hash over order ids and their bucket assignments), the
full bucket counts and whether that observation was clean.

Two consecutive runs sharing a fingerprint observed the same cohort in the same
state. `consecutive_clean_runs()` counts the current clean streak and **resets
on a contract-version change**, because runs with differing bucket semantics are
not comparable observations.

### Why repair is not automatic

Money repairs require finance approval and belong to their owner. A
disagreement does **not** establish which side is wrong: a stored column may be
correct while the metadata join is incomplete, which is precisely what this
phase exists to determine.

The check declares `SUPPORTS_APPLY = False` and **fails closed** when asked to
repair, raising `ShadowCheckCannotRepair`. A silent apply no-op would imply an
authority it deliberately lacks, so any shared CLI exposing `--apply` must read
that flag and refuse rather than quietly doing nothing.

## Cutover gate

Do not proceed to reads until all hold:

1. **No blocking bucket has any member** — `waived_evidence_missing`,
   `unlinked_unexpected`, `unresolved_invalid`, `unresolved_missing`,
   `unresolved_ambiguous` and `drifting` are all zero.
2. `unlinked_expected` is understood, with a documented reason for each
   remaining case.
3. **A consecutive clean observation window** of agreed length at one contract
   version — not a single green run.
4. **Complete structural writer coverage**: every new billing artifact arising
   from a sale is written with its structural link, with no path that produces
   an unlinked artifact.
5. **Verified historical backfill** of the structural links for existing rows.
6. **Validated constraints** — the structural links are enforced by the
   database, not by convention.
7. **Zero metadata fallback reads**: no code path still resolves the boundary
   through metadata.
8. **Architecture guards** preventing reintroduction of either a metadata join
   or a direct `SalesOrder` financial writer.
9. Finance sign-off on the resulting position for the active cohort.

Only then do `amount_paid`, `balance_due`, `payment_status` and `paid_at`
become reads, and only after that are the columns dropped and
`_apply_payment_fields` deleted.

**The full test suite cannot establish that the shadow joins are trustworthy.**
It validates the implementation; only the observation window against real data
establishes the cohort.

## Boundary tests owed at cutover

- A sales order's position equals the ledger's for every funding path: full
  payment, partial payment, deposit, waiver, discount.
- No writer outside `financial.*` assigns a money column on `SalesOrder`.
- The funding gate reads the position rather than a stored column.
