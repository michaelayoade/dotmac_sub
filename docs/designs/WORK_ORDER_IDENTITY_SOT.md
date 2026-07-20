# Work-order identity and native ownership

Status: implemented and verified, 2026-07-18. The eleven denormalized child
identifiers are retired, native work orders have no CRM provenance value,
authority is explicit, and compatibility response fields derive from
`public_id`. The persisted reconcile-job name remains while the CRM sync is
active; it identifies an integration process, not authoritative storage.

## Finding

`work_order_mirror` is not a mirror. It is Sub's only work-order storage, and
eleven field-evidence tables (worklogs, notes, attachments, chat, materials,
movements, fiber tests, job events, expenses, dispatch queue) hang off it with
no upstream to rebuild from. The table carries a cache's name, a cache's
reconcile affordances, and a foreign system's identifier as its unique key.

Three defects follow from that shape:

1. **Fabricated identity.** Native creation
   (`dispatch.WorkOrderHeaders.create`) must invent `sub-<uuid>` values and
   store them in `crm_work_order_id`, because the schema gives a natively
   created work order nowhere else to have an identity.
2. **Ownership by string prefix.** `is_sub_authoritative` decides whether CRM
   ingest may write status/timestamps by testing
   `crm_work_order_id.startswith("sub-")`.
3. **Denormalized foreign key.** All eleven evidence tables carry their own
   `crm_work_order_id` string column, NOT NULL and indexed, *in addition to* a
   UUID FK to the work-order row. Queries match the string; the FK is unused.
   A CRM-owned identifier is the join key for data CRM does not own — an
   imported identifier has become a copy of truth, which the source-of-truth
   standard forbids.

Unlike projects, quotes, and referrals, there is no separate native table and
therefore no authority transfer to run: no shadow owner, parity window, or
read-source flip.
Work orders need **re-keying, not migrating**.

## Decision

- `work_order_mirror` is renamed `work_order` (`WorkOrderMirror` →
  `WorkOrder`). It is authoritative storage and is documented and named as
  such.
- Identity is Sub-generated: `id` (UUID pk) plus `public_id` (unique, NOT
  NULL, human-safe). Existing rows backfill `public_id` from
  `crm_work_order_id`; new native rows receive `sub-<uuid>` (later: the
  numbering service, as `support_tickets.number` and `projects` already do).
- `crm_work_order_id` becomes a nullable unique provenance reference on
  `work_order` **only** — NULL for natively created rows, populated only by
  CRM import/webhook ingest. It is never a join key and never appears on child
  tables.
- Evidence tables join through their existing UUID FK. The denormalized
  string columns are dropped at the end of the sequence.
- `is_sub_authoritative` becomes `crm_work_order_id is None` (plus the
  existing `metadata.native_field_source == "sub"` marker for imported rows
  whose field activity Sub has taken over).
- CRM reconcile/webhook ingest resolves `crm_work_order_id → work_order.id`
  once at the boundary and operates on native identity thereafter. The
  Celery/beat names (`reconcile_work_order_mirror`,
  `work_order_mirror_reconcile`) are persisted identifiers of the *sync job*,
  not the table; they retire with the CRM integration and its scheduler rows.

## Identity pattern (applies to every module going Sub-native)

1. Identity is Sub-generated: UUID pk + human `number`/`public_id` from the
   numbering service. An external id is never the identity.
2. The external id is provenance: nullable, unique, on the aggregate root
   only, written only by the importer, dropped when the source system leaves.
3. Children join through the root's FK. A denormalized copy of the root's
   identity is a derived field with no canonical writer and no reconciler.
4. Importers resolve external → native at the boundary; owners decide on
   native identity.
5. A table holding the only copy of data is authoritative storage, whatever
   it is called. Name it for what it is and remove rebuild affordances that
   invite truncation.

Projects, quotes, and referrals are separate authority migrations and are out
of scope here, except that their native tables must conform to this pattern.

## Implementation record

1. **Authoritative identity.** The table/model rename and `public_id`
   column (backfilled `= crm_work_order_id`, then unique NOT NULL),
   `crm_work_order_id` made nullable on `work_order`, native create/get keyed
   on `public_id`. During the bounded transition, native creation also wrote
   the legacy CRM-shaped value while field consumers moved to native identity.
2. **Transport compatibility.** Field API paths and schemas accept legacy
   `crm_work_order_id` names while resolving the value as native `public_id`.
   This is a derived transport projection, not stored identity.
3. **Consumer verification.** The field application already keys jobs on the
   summary `id`, which the server sources from `public_id`; the customer mobile
   application has no dependency on the retired child identifier.
4. **Evidence re-keying.** Worklogs, notes, attachments, chat, materials,
   movements, fiber tests, job events, expenses, and dispatch queue queries
   resolve through `work_order.id` foreign keys.
5. **Legacy-field retirement.** The retirement removed the eleven denormalized
   columns and their indexes, changed compatibility response fields to derive
   `public_id`, NULLed fabricated CRM references on native rows, simplified the
   authority test, and updated `SOT_RELATIONSHIP_MAP.md`.
   The persisted sync job/beat name remains until CRM itself retires; it names
   an active integration job and is not an authority claim about the table.

## Verification gates

- The identity migration verifies that every legacy CRM identifier used to seed
  `public_id` is populated and unique.
- Query-equivalence tests prove that native-FK reads match the retired string
  joins on seeded imported and native work orders.
- The retirement migration fails before dropping a child identifier if it
  disagrees with the joined `work_order.public_id`.
- Legacy-shaped response fields derive from `public_id` after retirement.
- Architecture test: no model outside `app/models/work_order.py` declares a
  `crm_work_order_id` column.

## Non-goals

- Projects/quotes/referrals cutovers (existing plan, unchanged).
- Quote-deposit guard and native quote write wiring (separate scoped work).
- CRM-side changes. CRM remains a transport; its webhooks keep sending its
  ids, which Sub records as provenance.
