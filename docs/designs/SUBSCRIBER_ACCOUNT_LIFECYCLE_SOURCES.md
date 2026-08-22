# Subscriber account lifecycle — product-first inventory

Dated 2026-08-22. Required by `AGENTS.md` rule 22 and ADR-0006's extraction
amendment: inventory the fleet's existing implementations before adding shared
behaviour.

**Verdict: build no new module. The capability decomposes across two existing
Starter owners plus assembly-level coordination.**

## The capability

*Subscriber account lifecycle* covers:

1. Deletion request and execution state.
2. Restoration eligibility and precedence.
3. Affected resource **references and versions** — not copies.
4. Purge eligibility and terminal purge.
5. Retention and legal-hold decisions, and their audit evidence.

## Correction — the first version of this document was wrong

An earlier draft said Starter had **no owner** for any of this and proposed a
new module. That was wrong, and the reason is a method failure worth recording
rather than quietly replacing.

The inventory ran `ls packages/` in a Starter working tree **137 commits behind
`origin/main`**, which showed **24 packages**. `origin/main` has **71**, and the
committed ISP-essential candidate branch adds more. The search covered roughly a
third of the fleet and reported absence.

Two owners were missed. The proposed module would have been a **third** owner of
account lifecycle and a **second** owner of retention and disposition — the
precise outcome rule 22 exists to prevent.

This is the second "no declared owner" error in this domain in one session; the
first claimed addresses were unowned and was corrected in Sub PR #2619. Both
have the same shape: a partial search, then a conclusion of absence.
**Inventory `origin/main` and the committed candidate branches — never a local
working tree.**

## The owners that already exist

### `dotmac-customers` — account lifecycle

`audit-complete`, `product-first`, sourced from `dotmac_sub` including
`app/services/subscriber.py`. Declared owner: *"Tenant customer account
identity, narrow profile, lifecycle, and typed Party references"*. Contract:
*"Own customer account numbers, **account lifecycle**, display/profile
attributes…"*.

It takes concerns **1 and 2** — deletion request and execution state,
restoration eligibility and precedence. Those are close, restore and recovery
commands ON the customer account, which is what the module already says it owns.
Extending it is composition; a parallel module beside it would be a second
lifecycle authority.

### `dotmac-records` — retention, holds and disposition

`audit-complete`, on `origin/main`, naming `dotmac_sub` among its sources.
Declared owner covers *"retention schedules/triggers, legal holds,
preservation/custody and **disposition authority**"*. Its contract evaluates
disposition with source-state and all-hold rechecks, freezes batch membership,
binds an Approvals digest, and authorizes destruction only from matching
physical confirmation.

It takes concerns **4 and 5** — purge eligibility and terminal purge, retention
and legal-hold decisions with audit evidence. Sub computes a purge due date from
one integer setting (`restore_retention_days`) with no hold concept and no
approval; Records is a strictly stronger contract that already exists.

### The Sub assembly — coordination, not ownership

Concern **3** is not a module. The cascade spans nine owners and the
coordination between them is what an assembly is for — the same reason
`customer.experience_lifecycle` stays in the assembly.

Account recovery is therefore **not standalone**: Customers owns lifecycle,
Records owns retention and disposition, and the assembly coordinates the
affected domain owners through typed ports.

### A principle worth reusing

`dotmac-integration`'s `retention.py`: *"age out the CONTENT, never the
identity."* Deleting a receipt does not tidy it — it makes redelivery a **new**
event. That transfers exactly: purging a subscriber must not delete the record
that the subscriber existed and was purged, or a legacy re-import recreates the
account as new with none of the deletion decisions attached. A principle to
apply, not a package to compose.

## What ERP has

Two per-entity status flips, neither a candidate:

| Implementation | What it does |
|---|---|
| `fleet/vehicle_service.py::soft_delete` | Sets `Vehicle.status = DISPOSED`. Six lines. |
| `support/ticket.py::restore_ticket` | Clears a soft-delete flag on one ticket row. |

No cross-resource cascade, purge eligibility, retention window, legal hold,
restoration precedence or audit evidence. A search for snapshot-and-restore
anywhere in ERP returns nothing.

## What Sub implements today

`app/services/web_system_restore_tool.py` — and it is why this is not a
key-renaming exercise.

### The cascade writes nine resource classes, two ways

**Routes through an owner (2):** subscriptions via
`account_lifecycle.cancel_subscription`, service orders via
`service_order_lifecycle.restore_recorded_status`.

**Direct boolean flip — no owner, no event, no audit (7):** invoices, payments,
access credentials, RADIUS users, IP assignments, ONT assignments, splitter port
assignments.

Two of those seven are money. Five are scarce network resources whose
deactivation is what disconnects a customer and frees an IP, an ONT port and a
splitter port. `tests/architecture/test_ip_assignment_service_ownership.py`
already records the module as tolerated debt:
`"app/services/web_system_restore_tool.py",  # debt: restore tooling`.

### Three defects in the restore path

1. **Restore is asymmetric.** Subscriptions, service orders and CPE devices
   restore from the snapshot. The other six restore by blanket reactivation.

2. **Blanket reactivation resurrects rows the cascade never touched.** The
   cascade deactivates only invoices that were active, and counts them; the
   count is never used. Restore sets `is_active = True` on **every** inactive
   invoice, so an invoice voided for a legitimate business reason months before
   the deletion comes back active. Payments likewise.

3. **A missing snapshot silently rewrites service orders to `draft`.** The
   snapshot is read from the metadata blob behind an `isinstance(..., dict)`
   guard, so an absent, malformed or truncated value yields an empty dict and
   every service order is forced to `ServiceOrderStatus.draft`. Orders created
   after the snapshot was taken hit the same path. No error is raised.

**No behavioural tests exist for any of it.** `web_system_restore_tool` appears
in the test tree only in architecture baselines and as a named exception in
three boundary tests. Nothing calls `mark_subscriber_deleted`,
`restore_subscriber`, `purge_expired_from_recovery_queue` or
`build_restore_preview`; nothing exercises their three routes.

## `recovery_snapshot` is removed, not re-homed

It copies subscriptions, service orders and CPE devices onto the subscriber row,
after which the real rows are soft-deleted. A typed table for it would move the
defect: a second copy of resources whose owners already hold them, drifting from
the moment it is written.

Defects 2 and 3 share one root cause — **the system never recorded what it
actually changed.** It stored a picture of what things looked like, then guessed
on the way back. Affected-resource **references and versions** fix both: restore
only what this deletion deactivated, at the version it was deactivated from,
through the owning service.

This bounds recovery to before purge, deliberately. Recovery after purge
requires an explicit versioned archive with its own retention contract —
governed by Records, not a JSON column on the row being purged.

## Consequences

- **No new module.** Extend `dotmac-customers` with close, restore and recovery
  commands; use `dotmac-records` for deletion eligibility and purge
  authorization.
- Both need a completed rule-24 product-first dossier covering Sub's source and
  tests before the shared behaviour is implemented.
- The Sub assembly keeps thin port binding and orchestration across the nine
  affected owners. Two ports bind to existing owner commands; **seven owners
  have no deactivation command at all** and need one, which is where most of the
  work is.
- Characterization tests come first. The path is destructive, terminal and
  untested, so the extraction cannot claim to preserve behaviour it has never
  measured. The three defects are pinned as current behaviour and fixed as
  deliberate, separately visible changes.
