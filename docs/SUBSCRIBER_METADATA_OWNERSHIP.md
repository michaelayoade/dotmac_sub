# `subscribers.metadata` — ownership census

Dated 2026-08-22, measured by
[`scripts/architecture/subscriber_metadata_census.py`](../scripts/architecture/subscriber_metadata_census.py)
against Sub `dev`. This is a **census**, not a design for a metadata service.
The outcome of the work it scopes is that this column stops holding facts, not
that it gains a nicer front door.

## Why this is not "add a metadata service"

The obvious move — one typed facade in front of the blob, every writer routed
through it — is the wrong one. It would leave every fact in an unowned JSONB
column, add a single place where any feature can still write any key, and make
the wildcard write below look like an ownership boundary. A facade over
unbounded JSON mutation is unbounded JSON mutation with a docstring.

Each retained fact gets a **named owner and a typed command**. Each obsolete
fact gets deleted. What remains readable from the column is a rebuildable
compatibility projection, and nothing decides anything from it.

## Measured position

| | |
|---|---|
| Direct writer modules | ~~8~~ **7** (2026-09-13: `web_system_restore_tool` retired — see the update note below) |
| Read-only modules | **14** |
| Distinct keys | **34**, ~~less the 7 retired `recovery_*` keys~~ |
| Keys written by more than one module | 1 (`subscriber_category`) |
| Keys any admin can invent at runtime | **unbounded** — see the wildcard below |

### The count was seven, and seven was wrong

`docs/ISP_COHORT1_SOURCE_OWNERSHIP.md` recorded seven writers. The key-level
census finds **eight**, and the difference is not a tightening of definitions:

- **`app/services/subscriber.py` was missing.** The column's own declared
  owner service writes four `restricted_*` keys into it.
- **`app/services/web_customer_actions.py` was missing.** It writes seven
  notification-preference keys and carries the wildcard.
- **`app/services/web_customer_details.py` was counted and does not write.**
  It only reads `nin_verified` and `nin_last_checked_at`.

A file-level census cannot see any of that. It answers "does this file mutate
the column", which is the right question for a retirement ratchet and the wrong
one for ownership. The ratchet in this document therefore starts at **8**.

## The wildcard, which blocks everything else

`app/web/admin/customers.py` parses an admin form field `metadata_json` as
arbitrary JSON and hands it to `web_customer_actions`, which writes it wholesale:

```python
if metadata_json is not None:
    metadata_payload = dict(metadata_json)
    metadata_payload["subscriber_category"] = before.category.value
    data["metadata_"] = metadata_payload
```

Any admin can create any key with any value on any subscriber. **No ownership
assignment below survives this.** Assigning `recovery_deleted_at` to an owner
means nothing while a form field can set `recovery_deleted_at` to a string of
someone's choosing, and a typed command that validates its input is decorative
next to an endpoint that validates none.

This is the one item that must close before the others are worth doing, and it
closes by **deletion of the capability**, not by validation of it. There is no
legitimate operator need to invent a key on a customer record; every key that
matters is enumerated below and belongs to a service.

## Classification

Five classes. The class determines the remedy, so it is recorded per key rather
than per module — three modules write keys of more than one class.

### Authoritative state — has an owner, needs a typed home

Facts nothing else records. Losing them loses the fact.

| Key | Written by | Owner to hold it | Shape |
|---|---|---|---|
| `recovery_deleted_at`, `recovery_deleted_by`, `recovery_purge_due_at`, `recovery_purged_at`, `recovery_last_restored_at`, `recovery_last_restored_by`, `recovery_snapshot` | ~~`web_system_restore_tool`~~ **RETIRED** | **`customer.account_recovery`** (`app/services/account_recovery.py` — registered 2026-09-13) | n/a — typed `AccountRecoveryRecord` / `AccountRecoverySubscriptionSnapshot` rows now |
| `account_deletion_requested_at` | `account_deletion` | `customer.accounts` (typed column pending) | timestamp |
| `account_deletion_reason` | `account_deletion` | `customer.accounts` (typed column pending) | free text |
| `portal_read_notification_keys` | `customer_portal_notifications` | **`customer.portal_notifications`** | unbounded list — see below |
| 7 × `*_notifications`, `sms_updates` | `web_customer_actions` | **`customer.notification_policy`** (exists) | booleans |

Customer profile saves pass notification preferences through the typed
`SubscriberNotificationPreferencesUpdate` patch. `customer.accounts` merges only
that closed set of declared keys; it does not resubmit or silently remove unrelated
historical metadata while the preference facts await extraction to
`customer.notification_policy`.

The merge is staged in the existing subscriber update payload, not written to
an ORM row before validation. Lifecycle and billing-approval refusals leave the
row clean even before rollback. An explicitly supplied `metadata_` replacement
still passes the closed-key guard and keeps its replacement semantics; the typed
preference values are applied over that replacement in the same update. An
absent or null preference patch leaves existing metadata unchanged.

The optional `SubscriberUpdate.notification_preferences` API field is additive.
Its seven boolean fields reject extra keys. Regenerate the OpenAPI contract
manifest with `python scripts/update_openapi_contract.py` to record this
intentional shape; no route or existing required field changes. Regression
coverage lives in `test_subscriber_metadata_key_closure.py`
and `test_customer_portal_notifications.py`; the exact frozen import-key fixture
is retained in the existing `test_crm_portal_services.py` compatibility surface.
The cohort writer-site and vocabulary-freeze baselines are unchanged.

**Deletion lineage status.** The former `web_system_restore_tool` recovery
metadata lineage is retired; `customer.account_recovery` now owns typed
administrative recoverable-deletion evidence. The distinct self-service
`account_deletion_*` metadata lineage remains active and unmigrated. A
customer-requested permanent deletion must not become recoverable by sharing
the administrative recovery owner.

> **2026-09-13 update.** The `web_system_restore_tool` lineage (seven
> keys, including `recovery_snapshot`) moved
> to the newly-registered `customer.account_recovery` SOT owner (see
> `docs/SOT_RELATIONSHIP_MAP.md`), migration `607_account_recovery_evidence`
> backfilled every existing row into typed `AccountRecoveryRecord` /
> `AccountRecoverySubscriptionSnapshot` rows and removed the seven keys from
> `metadata_`, and `app/services/subscriber_metadata_keys.py` no longer
> declares them at all — a retired key is deleted from that registry, not
> relabeled. `web_system_restore_tool.py` is now a typed read/adapter layer
> with no `metadata_` access whatsoever. The `account_deletion_*` pair is a
> SEPARATE, still-active, still-unmigrated lineage: self-service deletion is
> never recoverable (no `AccountRecoveryRecord` is created for it), so it
> stayed out of `customer.account_recovery`'s scope — the earlier
> `customer.account_lifecycle` name below was aspirational and never became
> a registered owner. See `docs/designs/SUBSCRIBER_ACCOUNT_LIFECYCLE_SOURCES.md`
> for the full before/after.

**`portal_read_notification_keys` is an unbounded list inside a row.** Every
notification a customer reads appends an entry. It has no cap, no pruning and
no index, and it is rewritten in full on every read receipt. It is a join table
wearing a JSON array.

### Derived projection — rebuildable, must not be decided from

| Key | Written by | Derived from | Disposition |
|---|---|---|---|
| `nin_verified` | `nin_verifications` | `subscriber_nin_verifications` ledger | compatibility projection; readers repoint to the ledger |
| `nin_last_checked_at` | `nin_verifications` | same ledger | same |
| `restricted_since`, `restricted_status`, `last_restricted_status`, `last_restricted_ended_at` | `subscriber.py` | service restriction state | move to `access.subscription_lifecycle` |

`web_customer_actions` already **decides** from one of these:

```python
if bool((before.metadata_ or {}).get("nin_verified")) and data["nin"] != before.nin:
    data["nin"] = before.nin
```

A projection is refusing an edit to the authoritative column. That is the exact
failure mode item 6 guards against, and it is present today: the projection must
become read-only-for-display before the ledger can be trusted as the owner.

### Observation — a record that something was attempted

| Key | Written by | Owner |
|---|---|---|
| `geocode_attempted_at` | `customer_location_requests` | **`gis.spatial_sync`** — it is a geocode attempt, and that service owns coordinates as of PR #2620 |
| `crm_customer_name_remediation_digest` | `crm_customer_name_repair` | **`dotmac_kernel.idempotency`** — it is a replay marker, and at-most-once execution already has one owner (ADR-0014) |

### Integration payload — someone else's data, frozen

| Key | Read by | Source |
|---|---|---|
| `splynx_date_add`, `splynx_last_update` | `subscriber.py` | legacy Splynx import provenance |
| `splynx_deleted`, `splynx_status` | `customer_account_visibility` | same |
| `crm_person_id` | `cross_app_drift` | CRM provenance |

These stay as an opaque, frozen import record. Nothing writes them, they carry
no decisions the migration must reproduce, and they are read for provenance
only. They are the one class where remaining in a JSON blob is the correct
answer — but they belong in a clearly named provenance column, not mixed with
live state.

### Obsolete — delete, do not migrate

| Key | Why |
|---|---|
| `subscriber_category` | A JSON copy of `Subscriber.category`, a real typed column. Written by `web_customer_actions` (which stamps it from the column it duplicates) and read by **nine** modules. The duplicate exists so that readers can avoid a column read; the column is on the same row. |
| `latitude`, `longitude` | A **fourth** place an address coordinate lives, after `Address.latitude/longitude`, `Address.geom` and `GeoLocation`. Read by `web_customer_details` as a fallback when the column is null. `gis.spatial_sync` is the declared coordinate owner. |
| `send_billing_notifications` | Written beside `billing_notifications` by the same module. Two keys, one preference. |

### One key was found by the guard, not by the census

`auto_create_invoices` — a billing preference read by both billing presenters
through `getattr(subscriber, "metadata_", None)`.

The census originally excluded reflective access and said so in a "known limit"
section, reasoning that resolving `getattr` would mean matching on attribute
name again. That was wrong. A `getattr` whose attribute name is a **literal** is
exactly as static as the dotted form; only a computed name defeats analysis, and
nothing here computes one.

The closed-key registry built from the incomplete census then refused a write
the application legitimately makes, and the unit suite caught it. **A documented
gap is still a gap** — the census now resolves literal `getattr`, and
`latitude`/`longitude` became visible in the same change.

## The ratchet

[`tests/architecture/subscriber_metadata_writers_baseline.txt`](../tests/architecture/subscriber_metadata_writers_baseline.txt),
enforced by
[`test_subscriber_metadata_ownership.py`](../tests/architecture/test_subscriber_metadata_ownership.py).
Membership only, two-directional, starting at **8** and targeting zero.

Membership rather than magnitude is deliberate. A module either writes this
column or it does not; how many lines it takes says nothing about ownership, and
a site count would reward consolidating six writes into one loop over giving the
fact an owner.

The guard's load-bearing half is `test_every_receiver_is_classified`. A writer
behind a receiver the census cannot resolve escapes every other check, so an
unresolvable `<name>.metadata_` fails the build. Resolution is by binding —
annotation, construction, `db.get`, a query terminal, a loop over a query, or a
function's return annotation — **never by variable name**. Trusting names
reported twelve writers where there are eight, counting a `BrandProfile` blob
and an inbox conversation as subscriber facts, because half this codebase's
receivers are called `target`, `existing` or `record`.

## Order of work

1. **Close the wildcard.** Nothing else holds while it is open.
2. ~~**Extract account recovery** — the highest-risk writer, both deletion
   lineages, `recovery_snapshot`, and the purge sweep. Lower the ratchet 8 →
   7 in the same change.~~ **DONE 2026-09-13** for the `web_system_restore_tool`
   lineage (`customer.account_recovery`, ratchet lowered 8 → 7). The
   `account_deletion` lineage was explicitly out of scope for that
   extraction (self-service deletion is never recoverable) and remains
   open.
3. `portal_read_notification_keys` → a real table.
4. Notification preferences → `customer.notification_policy`, which exists.
5. `nin_*` → read from the ledger; the projection becomes display-only and the
   edit-refusal above moves to the ledger.
6. Delete `subscriber_category`, `latitude`, `longitude`,
   `send_billing_notifications`.
7. `restricted_*` → `access.subscription_lifecycle`.
8. `geocode_attempted_at` → `gis.spatial_sync`;
   `crm_customer_name_remediation_digest` → the kernel idempotency owner.
9. Splynx and CRM provenance → a named provenance column, frozen.

## `recovery_snapshot` is not metadata (historical — resolved 2026-09-13)

Called out separately because it was not a key like the others.
`web_system_restore_tool._build_snapshot` used to serialise a subscriber's
subscriptions, service orders and CPE devices — ids, statuses, cancellation
timestamps — into a JSON value on the subscriber row, and
`_apply_soft_delete_cascade` then soft-deleted the real rows. The snapshot was
the **only** record of what the account looked like before deletion, and
restoring read it back.

So it was recovery evidence carrying real referential meaning, held in a
column with no schema, no constraint, no foreign key and no size bound, on
the same row whose deletion it describes — it could not be validated, could
not be queried, and could not be repaired if it was wrong. This is exactly
why account recovery was the first conversion and not a later one.

**Resolution.** `app/models/account_recovery.py`'s
`AccountRecoverySubscriptionSnapshot` table replaces it: one row per
subscription per deletion generation, a real foreign key to
`subscriptions.id` (`RESTRICT`), a real foreign key to its parent
`account_recovery_records.id` (`CASCADE`), a `UNIQUE(recovery_record_id,
subscription_id)` constraint, and typed `pre_deletion_status` /
`pre_deletion_offer_version_id` columns instead of an untyped blob. Service
orders and CPE devices are NOT part of the new typed model at all —
`web_system_restore_tool.py`'s cascade code for them was removed outright,
not re-homed, because `customer.account_recovery` registers only
`subscription` as a supported recovery participant; a legacy row whose old
snapshot named a service order or CPE device is backfilled fail-closed (see
migration `607_account_recovery_evidence`) and reports
`blocked_missing_participants` rather than silently claiming it can restore
resources nothing owns anymore.
