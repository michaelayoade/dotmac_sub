# Subscriber account lifecycle — product-first inventory

Dated 2026-08-22, before any code is written. Required by `AGENTS.md` rule 22
and ADR-0006's extraction amendment: inventory the fleet's existing
implementations before adding shared behaviour, and port a qualifying
production implementation rather than inventing one.

**Verdict: extract to a Starter-owned module now, with Sub as consumer one.**

## The capability

*Subscriber account lifecycle* owns:

1. Deletion request and execution state.
2. Restoration eligibility and precedence.
3. Affected resource **references and versions** — not copies.
4. Purge eligibility and terminal purge.
5. Retention and legal-hold decisions, and their audit evidence.

## What exists in Starter

**No owner.** None of the 24 packages carries account or record lifecycle.
`dotmac-approvals`, `dotmac-ticketing`, `dotmac-projects` and the rest each hold
their own entity state; nothing holds the lifecycle of a subject *across*
owners, which is what deletion and restoration require.

One thing IS reusable, and it is a principle rather than code.
`dotmac-integration`'s `retention.py` states it exactly:

> Payload retention — age out the CONTENT, never the identity.

Its reasoning is that deduplication lives in a unique key, so a deleted receipt
is not a tidied receipt — it is a receipt whose redelivery becomes a **new**
event, processed a second time with no record that it was already answered.

That transfers directly. Purging a subscriber must not delete the record that
the subscriber existed and was purged, or a legacy re-import, a restored backup
or a provider replay recreates the account as new — a purged customer walking
back in through the import path, with none of the deletion decisions attached.
The lifecycle owner keeps identity and terminal state after purge and redacts
only content, for the same reason and by the same shape.

This is a principle to apply, not a package to compose: different domain,
different tables, no shared rows. Recorded so the reasoning is not re-derived.

## What exists in ERP

**Per-entity status flips only.** Two implementations, both narrow:

| Implementation | What it does |
|---|---|
| `app/services/fleet/vehicle_service.py::soft_delete` | Sets `Vehicle.status = DISPOSED`. Six lines. |
| `app/services/support/ticket.py::restore_ticket` | Clears a soft-delete flag on one ticket row. |

Neither is a candidate. Together they have no cross-resource cascade, no purge
eligibility, no retention window, no legal hold, no restoration precedence and
no audit evidence — which is five of the six concerns above. A search for a
snapshot-and-restore pattern anywhere in ERP returns nothing.

## What exists in Sub

`app/services/web_system_restore_tool.py` is the **only** implementation in the
fleet with the full shape: mark deleted → cascade soft-delete → compute a purge
due date from a retention setting → sweep and purge on expiry → restore.

It is therefore the product-first source, and it is also the thing being fixed:
it stores all of that in `subscribers.metadata`, an unowned JSONB column, and
serialises the affected resources into `recovery_snapshot`.

## What product-first actually requires

An earlier draft of this section concluded "build it in Sub, extract when a
second consumer exists". **That was wrong**, and the correction matters enough
to record rather than quietly replace.

Product-first means Sub supplies the **implementation and the parity tests**.
It does not mean Sub keeps ownership until someone else asks for it. The
Starter module becomes the owner, and Sub adopts it **in the same authority
slice** — one coherent change in which the fact moves to its owner and the
legacy writer retires, rather than two changes with a period of two writers
between them.

`dotmac_sub` is **consumer one**. A second consumer is what justifies further
GENERALISATION — widening a contract, adding a provider seam, admitting a shape
Sub does not need. It is not a precondition for the initial extraction. Waiting
for one would leave the fact in an unowned JSONB column indefinitely, since
nothing else is going to ask for subscriber account lifecycle first.

The risk the earlier draft named is real but differently managed: a module
designed from one caller can acquire that caller's private conventions. The
guard against that is the extraction dossier and the typed ports below — the
module owns lifecycle state and transitions, and everything Sub-specific stays
behind a port the assembly binds. It is not a delay.

## The snapshot is not re-homed, it is removed

`recovery_snapshot` currently copies subscriptions, service orders and CPE
devices — ids, statuses, cancellation timestamps — onto the subscriber row.
Giving that a typed table would move the defect, not fix it: a second copy of
resources whose owners already hold them, drifting from the moment it is
written, with no way to detect the drift.

The lifecycle owner records **references and versions** — which resources were
affected, at what version, by which deletion — and restoration goes back through
each owning service or its own soft-deleted rows. The owners already hold the
data; the lifecycle owner holds the decision.

This bounds recovery to before purge, deliberately. **If recovery after purge is
required, that is an explicit versioned archive with its own retention
contract** — a separate, named capability with its own storage and its own
rules. It is not `subscribers.metadata`, and it is not a JSON column on the row
being purged.

## Consequences for the extraction

- A new **Starter module** owning subscriber account lifecycle, with a complete
  `EXTRACTION.toml` written from Sub's source and tests.
- Durable lifecycle state and transitions live in the MODULE.
- The module exposes **typed ports** for subscriptions, orders, devices and
  every other affected owner, and imports no sibling module.
- The Sub assembly keeps only thin port binding and orchestration, and declares
  `customer.account_lifecycle` in the SOT registry as the local owner name.
- Both deletion lineages move to it: `account_deletion`'s
  `account_deletion_requested_at`/`_reason` and the restore tool's
  `recovery_deleted_at`/`_by`/`_purge_due_at`/`_purged_at`/`_last_restored_at`/
  `_by`. They record the same event today with no rule about which wins; the
  owner is where that rule lives.
- `recovery_snapshot` is replaced by typed affected-resource references, and
  the purge sweep moves with them.
- The `subscribers.metadata` writer ratchet falls by however many modules stop
  writing the column — measured by the scanner, not asserted.
