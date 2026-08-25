# ADR-0011: Prepare adoption of `dotmac-billing` as operational-receivables owner

**Status:** Accepted for preparation; production authority switch not authorized
**Date:** 2026-08-18
**Decision owner:** Michael
**Relates to:** ADR-0007; Starter ADR-0017, ADR-0020, ADR-0023,
ADR-0024, ADR-0030; and
`docs/runbooks/DOTMAC_BILLING_TENANT_ADOPTION.md`.

## Context

ADR-0007 gave Sub-local services the target shape for contracts, obligations,
invoices, confirmed cash, allocations, immutable posting groups, and
per-currency positions. Starter ADR-0020 subsequently assigned the reusable
operational-receivables boundary to one installable module,
`dotmac-billing`. ERP remains the only GL/statutory-accounting owner and the
Integrator remains the only external payment-transport owner.

Sub already has live invoice, settlement, allocation, balance, and tax paths.
Installing another writer beside them would create two financial authorities.
The adoption must therefore separate isolated target rehearsal from the later
single coupled production switch.

## Decision

1. Sub prepares to consume `dotmac-billing` on its **tenant plane only**. The
   platform plane is absent from this assembly; a fake, nullable, or sentinel
   tenant is forbidden.
2. Until an explicitly authorized production cutover, the existing executable
   SOT registry and Sub-local writers remain authoritative. The candidate is
   not imported by `app.main`, `app/composition.py`, Sub's Alembic environment,
   routes, tasks, jobs, workers, or webhooks.
3. Preparation uses the standalone package under `adoption/dotmac_billing`.
   It pins the exact candidate versions, composes only the kernel and Billing
   lineages in a distinct disposable database, disables product routes and
   outbound delivery, and invokes Billing's published typed commands. It owns
   migration evidence only; it is not another invoice, payment, allocation,
   balance, tax, provider, or accounting owner.
4. Cross-component boundaries are immutable and fully typed. Sub mapping
   inputs become the published `AcceptRatedObligationV1` and
   `AcceptSettlementV1` contracts. Reconciliation, source disposition,
   readiness, and watermark evidence use immutable typed V1 contracts with
   stable domain errors. There is no `Any` or free-form payload boundary.
5. Every legacy fact receives exactly one disposition: target backfill,
   provider-owned projection, closed archive, cutover blocker, or known
   incorrect native fact. Unknown/unverified due-date basis is lawful and
   reportable but cannot authorize automated collection.
6. Shadow execution is not a dual write. It replays captured immutable source
   evidence into the isolated Billing database while the legacy Sub writer
   remains the sole live authority. Legacy, Billing, and an independent control
   are compared exactly; no money tolerance exists.
7. Invoice, confirmed-settlement, and allocation authority may switch only at
   one coupled watermark. The gate requires total source classification, three
   distinct complete reconciliations, exact position rebuild hashes, tenant RLS
   and wrong-plane proofs, an exact retirement ratchet, and the inbound
   transport checkpoint. After the first post-watermark Billing fact, recovery
   is roll-forward through reversing/correcting facts.
8. Durable Timers is an upstream prerequisite for effective recurring-charge
   adoption, not a Billing runtime dependency. Subscriptions/Timers own cadence
   and occurrence scheduling; Billing accepts the resulting rated obligation.
   The current timer worktrees are revision-pinned read-only evidence and are
   not edited by this adoption.
9. This preparation does not implement or absorb Subscriptions, Collections,
   Numbering, Document Rendering, Files, provider connectors, product access
   consequences, ERP posting, GL, journals, fiscal periods, treasury, or tax
   returns.

## Authority boundary

| Concern | Before the coupled switch | After a separately authorized switch |
| --- | --- | --- |
| Recurring offer, contract version, cadence, proration, occurrence | Sub-local subscription/billing-contract owners | `dotmac-subscriptions` when separately adopted |
| Rated-obligation acceptance, invoice/credit lifecycle, confirmed settlement, allocation and operational position | Current Sub owners declared by the executable registry | `dotmac-billing` tenant plane |
| Delinquency policy and consequence request | Current Sub collections owners | `dotmac-collections` when separately adopted |
| PSP credentials, provider clients, verification, retry, checkpoint | Current migration debt pending Integrator adoption | Integrator only |
| GL, statutory accounting, journals, fiscal periods, tax return | ERP | ERP |
| Service/access consequence | Sub product owners | Sub product owners |

The second column remains current reality. The third column is a cutover target,
not a claim that adoption or production migration has happened.

## Migration and verification

- S0 classifies the complete legacy financial cohort before mapping.
- S1 runs the isolated tenant-plane graph, mapping, replay, and exact
  three-way reconciliation.
- S2 may prepare backfill batches only after blockers are resolved by their
  current owner. A backfill never emits a new ERP accounting fact for historical
  rows and never turns provider observations into native Billing facts.
- S3 records the coupled maintenance/watermark procedure and retirement gates.
- S4, the production authority switch, requires separate authorization and is
  deliberately outside this change.

The architecture and PostgreSQL canaries prove exact package pins, no production
composition, tenant-only selection, ENABLE+FORCE RLS, exact grants, two-tenant
isolation, platform-role refusal, coupled watermark coherence, exact
reconciliation, and two-directional retirement sensitivity.

The executable SOT registry is intentionally unchanged in this preparation.
Changing its production owners now would be a false cutover claim. It must be
updated atomically with the eventual authorized authority switch and the
retirement of the displaced writers.

## Rollback and forward-fix

Before the coupled switch, discard or rebuild only the isolated target database;
Sub production authority is unchanged. No legacy row is deleted.

During the maintenance pause, a technical rollback may re-enable the complete
legacy writer set only before Billing accepts its first post-watermark fact.
After that fact exists, do not restore legacy writers or mutable balances.
Correct forward through immutable deallocation, refund, reversal, credit, or
document supersession evidence and rerun reconciliation.

## Retirement condition

This ADR is complete only when the production cutover is separately approved,
the executable registry names the external Billing owner, all five displaced
authority families have reached zero under the two-directional ratchet, and no
fallback writer or legacy balance authority remains. Historical evidence may be
retained read-only; retained data does not retain authority.
