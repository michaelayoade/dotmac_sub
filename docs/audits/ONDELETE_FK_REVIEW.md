# FK `ondelete` Delete-Safety Review (2026-06-26)

Audit of every foreign-key delete behavior in `app/models/`, reviewed from the
**parent-delete story**: when the parent row is deleted, what happens to the child?

Policy: `CASCADE` is good for pure dependents (join tables, tokens, ephemeral
state, observations). It is **bad** for invoices / payments / ledger / audit /
sessions / network inventory and any **active** record — those should `RESTRICT`
(block the delete), soft-delete, or `SET NULL` only when nulling is harmless.

## Inventory

| Behavior | Count | Notes |
|---|---|---|
| (default) NO ACTION | 381 | Safe — Postgres blocks parent delete while children exist. |
| `CASCADE` | 50 | Most are pure dependents; a few are inventory/audit (below). |
| `SET NULL` | 28 | Most are optional/denormalized refs; 3 orphan **active** records. |

Financial/audit/session tables are clean: `invoices`, `payments`,
`ledger_entries`, `billing_accounts`, `audit_events`, `event_store`, and auth
`sessions`/`api_keys`/`user_credentials`/`mfa_methods` all use the default
NO ACTION FK — never CASCADE/SET NULL. The protective defaults also block the
dangerous parent deletes: `Subscription.subscriber_id`, `Invoice.subscriber_id`,
`Payment.subscriber_id` are all NO ACTION, so a subscriber with subscriptions or
financials **cannot** be hard-deleted.

## Findings (ranked)

### HIGH — reachable, orphans/wipes a must-preserve record

| # | Location | FK | Today | Parent-delete story | Recommended |
|---|---|---|---|---|---|
| H1 | `network.py:539` | `ip_assignments.subscriber_id → subscribers` | SET NULL | Subscriber hard-delete (`subscriber.py:772`) nulls the owner but leaves `is_active=true` → orphaned active assignment; the partial-unique index keeps the IP locked, unreclaimable. **IP leak.** | Release (deactivate) assignments in the delete flow **before** delete; keep SET NULL as backstop. |
| H2 | `network.py:544` | `ip_assignments.subscription_id → subscriptions` | SET NULL | Same as H1 at subscription granularity. | Same as H1. |
| H3 | `network.py:335` | `cpe_devices.subscriber_id → subscribers` | SET NULL | Active CPE (`status=active`) loses its owner on subscriber delete → orphan device in inventory. | Release/retire CPE in the delete flow before delete. |
| H4 | `mrr_snapshot.py:49` | `mrr_snapshots.subscriber_id → subscribers` | CASCADE | Subscriber hard-delete **wipes revenue snapshots** — financial history loss. Reachable (only when no subscriptions/invoices block the delete). | `SET NULL` (preserve the snapshot, unlink owner) — make column nullable. |

### MEDIUM — reachable, deletes operational/compliance state

| # | Location | FK | Today | Parent-delete story | Recommended |
|---|---|---|---|---|---|
| M1 | `enforcement_lock.py:88` | `enforcement_locks.subscription_id → subscriptions` | CASCADE | Subscription hard-delete (`subscriptions.py:1725`) wipes suspension/lock audit. | Leave CASCADE (operational state) OR SET NULL if the lock history is needed for compliance — product call. |

### LOW — dormant time-bombs (network inventory, soft-delete-guarded)

The OLT/ONT inventory parents are **soft-delete only** — `olt_device_crud.py`
blocks deletion while active ONTs/assignments exist, so these CASCADEs never
fire today. But they are a latent hazard: a future hard-delete (or a test) would
silently wipe active inventory/allocations. Harden defensively to RESTRICT:

| # | Location | FK | Today | If parent ever hard-deleted |
|---|---|---|---|---|
| L1 | `network.py` `ServicePortAllocation.ont_unit_id` | → `ont_units` | CASCADE | wipes active service-port allocations |
| L2 | `network.py` `OntWanServiceInstance.ont_id` | → `ont_units` | CASCADE | wipes resolved WAN service configs |
| L3 | `network.py` `OltServicePort.olt_device_id` | → `olt_devices` | CASCADE | wipes imported service-port inventory |
| L4 | `network.py` `OltOntRegistration.olt_id` | → `olt_devices` | CASCADE | wipes ONT registration cache |

The remaining ~12 network CASCADEs (observations, config snapshots, profile
bundles/maps, group membership, utilization snapshots, router interfaces/backups)
are **correct** — pure observation/config dependents.

### Confirmed SAFE

- 25 of 28 `SET NULL` FKs are optional/denormalized refs (pop_site, vlan, OLT
  pointers, notification→subscriber, self-referential parent links, FUP rule
  chaining) — nulling is harmless.
- The reseller cluster is safe: `Subscriber.reseller_id` is **NO ACTION** (a
  reseller with customers can't be deleted); the only reseller CASCADEs are
  `ResellerUser` (portal logins) and `OfferResellerAvailability` (config), both
  correct pure dependents.

## Recommended change set

1. **Defensive migration (no flow risk):** L1–L4 `CASCADE → RESTRICT`.
2. **H4 `mrr_snapshots.subscriber_id`:** `CASCADE → SET NULL` + make column
   nullable, so revenue history survives a subscriber delete.
3. **H1–H3 (needs delete-flow code):** release/deactivate active IP assignments
   and CPE in the subscriber-delete flow before the delete, so SET NULL only ever
   nulls already-inactive rows. This is the IPAM "asymmetric release" leak fix.
4. **M1:** product decision (keep CASCADE vs preserve lock history).
