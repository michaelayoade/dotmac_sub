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
the ledger and compares it with the stored columns. It writes nothing.

The sales lifecycle reconciler runs the scan in **both** detect and apply mode
and reports four counts:

- `sales_orders_scanned`
- `sales_orders_unlinked_to_billing`
- `sales_orders_with_unresolved_billing_joins`
- `sales_orders_drifting_from_billing`

Each drift is logged at WARNING with the field, the stored value and the ledger
value.

Deliberate exclusions, so the signal stays honest:

- **Waived orders are not compared.** A waiver is settled by decision, not by
  money; the ledger legitimately shows nothing settled, and comparing would
  report drift on every waiver.
- **Unlinked orders are counted separately, not as drift.** An order billing
  cannot see at all is a join problem, not a disagreement. Conflating them
  would make the drift number meaningless.
- **Unresolved joins are counted separately** — a metadata id pointing at a
  missing or non-UUID invoice is evidence the metadata join is unsafe, which is
  the argument for the structural phase.

### Why repair is not automatic

Money repairs require finance approval and belong to their owner. A
disagreement here does **not** establish which side is wrong: a stored column
may be correct while the metadata join is incomplete, which is precisely what
the shadow phase exists to determine. The reconciler therefore reports and
never writes, in apply mode as much as in detect mode.

## Cutover gate

Do not proceed to reads until all hold:

1. `sales_orders_with_unresolved_billing_joins` is zero — the join is sound.
2. `sales_orders_unlinked_to_billing` is understood; every remaining case has a
   documented reason (for example a draft order with no invoice yet).
3. `sales_orders_drifting_from_billing` is zero across the active cohort.
4. Structural foreign keys have replaced the metadata joins.
5. Finance has signed off on the resulting position for the active cohort.

Only then do `amount_paid`, `balance_due`, `payment_status` and `paid_at`
become reads, and only after that are the columns dropped.

## Boundary tests owed at cutover

- A sales order's position equals the ledger's for every funding path: full
  payment, partial payment, deposit, waiver, discount.
- No writer outside `financial.*` assigns a money column on `SalesOrder`.
- The funding gate reads the position rather than a stored column.
