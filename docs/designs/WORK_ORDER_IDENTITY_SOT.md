# Work-order identity and native ownership

Status: slices 1–4 implemented (one PR), 2026-07-16. Slice 5 pending its gate.

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
therefore no cutover to run: no shadow phase, no parity check, no read flip.
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
  not the table; they are renamed only in the final slice, with scheduler
  rows migrated.

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

Projects, quotes, and referrals are true mirrors beside dark native tables;
they keep the existing 12-step cutover plan and are out of scope here, except
that their native tables must be born conforming to this pattern.

## Sequence

Slices 1–4 shipped together in one PR (decision 2026-07-16: complete module
over stacked slices). Slice 5 ships separately once its gate clears.

Slice 3 resolved as a verified no-op: the field app keys jobs on the summary
``id`` field and never reads ``crm_work_order_id``; the server now sources
``id`` from ``public_id`` with identical values, so old and new app builds
work before, during, and after slice 5 with no client change or release.
The customer app (`mobile/`) likewise has zero references.

1. **Rename + identity (this branch).** Table/model rename, `public_id`
   column (backfilled `= crm_work_order_id`, then unique NOT NULL),
   `crm_work_order_id` made nullable on `work_order`, native create/get keyed
   on `public_id`. Native creation **dual-writes** `crm_work_order_id =
   public_id` for the duration of the compat window, because field lookups and
   evidence rows still key on it; `is_sub_authoritative` gains the
   `crm_work_order_id IS NULL` marker but keeps the `sub-` prefix test until
   slice 5. Child tables untouched; their NOT NULL string columns still
   receive the same values, so nothing observable changes.
2. **API compat window.** Field API paths and schemas accept and emit both
   `crm_work_order_id` and `public_id` (same value today, so this is aliasing,
   not dual-write). New field-app builds read `public_id`.
3. **Field app release.** `field_mobile` switches to `public_id`; ship via
   the normal mobile release pipeline; wait out adoption.
4. **Evidence re-keying, one domain per PR (~10 PRs).** worklogs → notes →
   attachments → chat → materials → movements → fiber → job events →
   expenses → dispatch queue. Each PR moves that domain's queries from
   string-match to FK join. The string column keeps being written until
   slice 5, so every PR is revertible in isolation.
5. **Retirement.** Drop the eleven denormalized columns and their indexes,
   remove the API alias once field-app adoption clears, rename the sync
   job/beat identifiers with a scheduler-row migration, and update
   `SOT_RELATIONSHIP_MAP.md` to name `work_order` authoritative with
   `crm_work_order_id` as provenance.

## Gates

- Slice 1 migration asserts, before altering anything, that no two rows share
  a `crm_work_order_id` and none is NULL/empty (it is the `public_id` seed).
- Slice 4 PRs each carry a query-equivalence test: string-match and FK join
  return identical row sets on seeded fixtures, including the native
  (`crm_work_order_id IS NULL`) case.
- Slice 5's original field-app-adoption gate is void (no client ever read the
  alias — see slice 3). Its remaining gate: the retirement migration fails
  before dropping columns if any child string value disagrees with the joined
  `work_order.public_id`, and the API alias is removed in the same change.
- Architecture test: no model outside `app/models/work_order.py` declares a
  `crm_work_order_id` column (added at slice 5; the inverse baseline until
  then).

## Non-goals

- Projects/quotes/referrals cutovers (existing plan, unchanged).
- Quote-deposit guard and native quote write wiring (separate slices already
  identified).
- CRM-side changes. CRM remains a transport; its webhooks keep sending its
  ids, which Sub records as provenance.
