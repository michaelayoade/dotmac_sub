# Subscriber account lifecycle — product-first inventory

Dated 2026-08-22, before any code is written. Required by `AGENTS.md` rule 22
and ADR-0006's extraction amendment: inventory the fleet's existing
implementations before adding shared behaviour, and port a qualifying
production implementation rather than inventing one.

**Verdict: build it in Sub, product-first. Do not create a shared module yet.**

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

## Why a shared module would be premature

Rule 22 asks for a qualifying production implementation and a real second
consumer. There is one implementation, in Sub, and no second consumer: ERP's
two status flips would not adopt a lifecycle owner, and Starter has no product
asking for one. Extracting now would mean designing a shared contract from a
single caller — which is how a module acquires that caller's private
conventions and calls them a contract.

The order is: build it in Sub as a Sub-owned service with a typed contract,
prove it against the real deletion and purge paths, and extract to Starter when
a second consumer exists. Sub becoming a thin assembly does not change this —
under the conversion amendment, Sub composes released modules, and a module
released from one caller's assumptions is the failure that amendment did not
remove.

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

- One new Sub service, `customer.account_lifecycle`, declared in the SOT
  registry with typed commands.
- Both deletion lineages move to it: `account_deletion`'s
  `account_deletion_requested_at`/`_reason` and the restore tool's
  `recovery_deleted_at`/`_by`/`_purge_due_at`/`_purged_at`/`_last_restored_at`/
  `_by`. They record the same event today with no rule about which wins; the
  owner is where that rule lives.
- `recovery_snapshot` is replaced by typed affected-resource references, and
  the purge sweep moves with them.
- The `subscribers.metadata` writer ratchet falls by however many modules stop
  writing the column — measured by the scanner, not asserted.
