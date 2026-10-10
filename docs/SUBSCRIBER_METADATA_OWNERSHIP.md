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
| Direct writer modules | **9** |
| Read-only modules | **15** |
| Distinct keys | **36** |
| Keys written by more than one module | 1 (`subscriber_category`) |
| Keys any admin can invent at runtime | **unbounded** — see the wildcard below |

### The measured writer set has grown

`docs/ISP_COHORT1_SOURCE_OWNERSHIP.md` recorded fewer writers than the current
key-level census, which finds **nine**:

- **`app/services/subscriber.py` was missing.** The column's own declared
  owner service writes four `restricted_*` keys into it.
- **`app/services/web_customer_actions.py` was missing.** It writes the
  `subscriber_category` and `send_billing_notifications` projections and still
  carries the admin metadata wildcard.
- **`app/services/customer_portal_profile_commands.py` now writes the seven
  closed notification-preference keys** inside the registered typed profile
  command.
- **`app/services/web_customer_details.py` was counted and does not write.**
  It only reads `nin_verified` and `nin_last_checked_at`.

A file-level census cannot see any of that. It answers "does this file mutate
the column", which is the right question for a retirement ratchet and the wrong
one for ownership. The current membership ratchet therefore starts at **9**.

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
| `recovery_deleted_at` | `web_system_restore_tool` | **`customer.account_lifecycle`** (new) | timestamp |
| `recovery_deleted_by` | `web_system_restore_tool` | `customer.account_lifecycle` | actor id |
| `recovery_purged_at` | `web_system_restore_tool` | `customer.account_lifecycle` | timestamp, terminal |
| `recovery_last_restored_at` | `web_system_restore_tool` | `customer.account_lifecycle` | timestamp |
| `recovery_last_restored_by` | `web_system_restore_tool` | `customer.account_lifecycle` | actor id |
| `account_deletion_requested_at` | `account_deletion` | `customer.account_lifecycle` | timestamp |
| `account_deletion_reason` | `account_deletion` | `customer.account_lifecycle` | free text |
| `portal_read_notification_keys` | `customer_portal_notifications` | **`customer.portal_notifications`** | unbounded list — see below |
| 7 × `*_notifications`, `sms_updates` | `customer_portal_profile_commands` | **`customer.notification_policy`** (exists) | booleans |

Customer portal profile saves validate notification preferences as typed command
fields and persist only their seven declared keys inside the registered
`customer.portal_profile_commands` transaction. The keys remain in Subscriber
metadata as a compatibility projection until extraction to
`customer.notification_policy`; the command preserves unrelated historical
metadata when it updates those preference values.

The command validates preference booleans before opening its owner-managed
transaction, then updates only the explicit registered keys on the locked
Subscriber. It preserves unrelated historical metadata. Validation or scope
refusals leave the row unchanged.

The seven typed profile command fields reject unknown preference keys. The
registered owner and behavior coverage are recorded in
`customer.portal_profile_commands` and `test_customer_portal_notifications.py`.
The exact frozen import-key fixture remains in `test_crm_portal_services.py` for
its compatibility surface.

**Two deletion lineages, one lifecycle.** `account_deletion` writes
`account_deletion_*`; `web_system_restore_tool` writes `recovery_deleted_*`.
They record the same event — this account was deleted — in different key
families, written by different modules, with no relationship between them and
no rule about which wins. That is the strongest single argument for extracting
**subscriber account lifecycle** as its own state rather than tidying the keys
in place.

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

`web_subscriber_details` also still reads three undeclared import keys as
display fallbacks — `splynx_last_online`, `splynx_gps` and
`splynx_location_id` — so they remain on the rows that carry them until that
reader is retired.

### Moved — `splynx_billing_email`, now read-only provenance

The Splynx billing address has a typed home: a `subscriber_contacts` row
flagged `is_billing_contact`, which is what `communication_intents` narrows
billing mail to (the account's own `subscribers.email` is always included, and
is what invoices and receipts address). Migration
`665_backfill_splynx_billing_email_contacts` re-derives each row at apply time
with the contact owner's normalisation and:

- **backfills** a billing contact (`receives_notifications = false`, so no
  delivery changes) when the account has no billing contact, then removes the
  key;
- **removes** the key when it already equals the account email or one of the
  account's billing-contact emails;
- **leaves untouched** rows where it conflicts with a different, non-empty
  billing-contact email, and rows whose value is not one valid address.

One `audit_events` row (`subscriber.splynx_billing_email_moved`) records the
counts (`backfilled`, `removed`, `conflicts`, `invalid`) and the subscriber
ids of conflict and invalid rows for human review — no email addresses.

The detail-page reader now uses `customer_portal_contacts.account_billing_email`
only. Because conflict and invalid rows may still carry the key, it is not
retired: `PROVENANCE_ONLY_SPLYNX_KEYS` in
`scripts/architecture/subscriber_metadata_census.py` declares it as frozen,
read-only provenance. It is not in the writable registry, and
`test_subscriber_metadata_ownership.py` fails if product code reads or writes
it again. Once the reviewed rows are resolved and no row carries it, move it to
`RETIRED_SPLYNX_KEYS` with a purge migration.

### Retired — purged from every row

Migration `664_purge_retired_splynx_metadata_keys` removed these undeclared,
unread Splynx import keys from `subscribers.metadata` without copying their
values anywhere (irreversible by design; one `audit_events` row,
`subscriber.metadata_keys_purged`, records the key list and per-key row
counts):

`splynx_login`, `splynx_category`, `splynx_partner_percent`,
`splynx_billing_type`, `splynx_added_by`, `splynx_added_by_id`,
`splynx_customer_labels`, `splynx_daily_prepaid_cost`, `splynx_gdpr_agreed`,
`splynx_email`, `splynx_email_conflict`, `splynx_conversion_date`,
`splynx_password_cleartext`.

`RETIRED_SPLYNX_KEYS` in `scripts/architecture/subscriber_metadata_census.py`
holds the same list, and `test_subscriber_metadata_ownership.py` fails if any of
them is declared again or referenced by product code under `app`, `scripts`,
`templates` or `static`.

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
Membership only, two-directional, starting at **9** and targeting zero.

Membership rather than magnitude is deliberate. A module either writes this
column or it does not; how many lines it takes says nothing about ownership, and
a site count would reward consolidating six writes into one loop over giving the
fact an owner.

The guard's load-bearing half is `test_every_receiver_is_classified`. A writer
behind a receiver the census cannot resolve escapes every other check, so an
unresolvable `<name>.metadata_` fails the build. Resolution is by binding —
annotation, construction, `db.get`, a query terminal, a loop over a query, or a
function's return annotation — **never by variable name**. Trusting names
reported twelve writers where there are nine, counting a `BrandProfile` blob
and an inbox conversation as subscriber facts, because half this codebase's
receivers are called `target`, `existing` or `record`.

## Order of work

1. **Close the wildcard.** Nothing else holds while it is open.
2. **Extract account recovery** — the highest-risk writer, both deletion
   lineages, `recovery_snapshot`, and the purge sweep. Lower the ratchet 9 → 8
   in the same change.
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

## `recovery_snapshot` is not metadata

Called out separately because it is not a key like the others.
`web_system_restore_tool._build_snapshot` serialises a subscriber's
subscriptions, service orders and CPE devices — ids, statuses, cancellation
timestamps — into a JSON value on the subscriber row, and
`_apply_soft_delete_cascade` then soft-deletes the real rows. The snapshot is
the **only** record of what the account looked like before deletion, and
restoring reads it back.

So it is recovery evidence carrying real referential meaning, held in a column
with no schema, no constraint, no foreign key and no size bound, on the same row
whose deletion it describes. It cannot be validated, cannot be queried, and
cannot be repaired if it is wrong. Extracting account recovery means giving this
a typed home with real references — which is why account recovery is the first
conversion and not a later one.
