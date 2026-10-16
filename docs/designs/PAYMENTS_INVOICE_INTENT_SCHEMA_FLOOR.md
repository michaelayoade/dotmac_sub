# Prospective Payments invoice intent correlation

Status: source preparation; migration, release and activation are unapproved.

The accepted Starter tree at `0786296ac2a18b40569ea4f31447600dafd10d8f`
(same tree as release source `4fc0295da94ce62919966baaf1dd909f9af5dcbe`)
defines `mod_payments.payment_intents` with UUID primary key `id` and unique
`(tenant_id, id)` in `pm_0001_payment_intents`. The composed dependency is the
released head `pm_0003_confirmation_immutable`. No released migration is edited.

`691_payments_intent_correlation` adds nullable UUID `module_intent_id` and
`module_intent_tenant_id` to `public.topup_intents`. A paired-null check and
composite foreign key reference that published identity. Sub's existing
operator tenant owner supplies the tenant; the local table has no tenant column.
These are correlation facts, not another Payments authority. Public ORM metadata
does not import module persistence models. Constraints remain migration-owned.
Historical rows stay null and retain status, reconciliation and completion
updates; no backfill or financial history mutation occurs. Linking history by
UPDATE is refused.
An unlinked row cannot be reclassified from a non-invoice gateway flow into a
gateway invoice flow by UPDATE. The same structural and compatibility detection
is applied to OLD and NEW, including direct-transfer/prepaid exclusions; provider,
channel and metadata changes cannot bypass the prospective INSERT fence.

The INSERT fence detects gateway invoice rows from structural `invoice_id`;
existing `payment_flow`/`invoice_id` metadata also detects legacy incomplete
writers. Metadata cannot supply missing structural identity for new linked rows.
Direct bank transfer, reseller card and prepaid purchase creation retain their
existing unlinked paths. A linked row requires account, invoice, paired module
identity and current operator tenant. FK validation refuses orphan/wrong-tenant
module identity. Both linked rows and historical unlinked gateway invoice rows
retain their original frozen checkout binding and cannot be rebound or deleted.
Historical references cannot be erased or repurposed as fresh legacy checkout.
Lifecycle fields and observation metadata remain mutable. Noninvoice declared
flows retain their existing behavior. A caller inserts the released module record, local projection and event
on the same caller-owned Session; neither floor nor schema owns financial logic.

`app.services.payments_intent_schema_floor` owns only operational observation
and the closed `PREDECESSOR` / `INSTALLED` / `UNKNOWN` floor classification.
Its frozen `PaymentsIntentSchemaObservation` records
catalog evidence counts and exact installed shape. The SELECT-only reader
requires the actual stable nonprivileged LOGIN, checks catalog value types, UUID
columns, paired-null check, exact tenant FK, enabled INSERT/UPDATE/DELETE guard
and pinned function body. No row counts, tenant-filtered business reads, flags,
metrics or separate database determine the floor. Complete predecessor shape
and zero installation evidence alone admit the incumbent image. Partial,
malformed and unknown evidence fail closed. Installation refuses downgrade even
before first use; recovery is same digest or a reviewed forward fix.

Normal forward deployment has a separate support admission: complete
predecessor shape and complete installed shape are both understood, so either a
clean predecessor or the exact installed catalog shape can proceed to existing
migration/head checks. This proof runs before producer shutdown and again after
drain immediately before DDL. The predecessor-only payment rollback operation
remains recovery-only; it cannot certify a supported installed catalog.
Post-migration admission requires both the installed correlation shape and its
complete predecessor/base catalog. Any partial or unclassified catalog refuses.

This floor contract alone is not deployable. Deployment source requires the
forward support classification around writer drain and retains predecessor-only
checks at automatic recovery gates. Real hosted Docker, PostgreSQL and
interruption canaries remain required before source acceptance. Michael must
approve concrete writer/barrier growth, a distinct scoped feature exception,
migration and release before activation. PR3640's read-only exception does not
authorize this prospective writer.

Hosted validation sources: `tests/test_payments_intent_schema_floor.py` covers
decision/malformed-fact refusal and migration-body binding;
`tests/integration/test_payments_intent_correlation_pg.py` upgrades the real
predecessor through composed heads and exercises actual runtime-login grants,
legacy insert refusal, null-history lifecycle, linked immutability, invalid FK
scope, transaction rollback and partial/malformed installed catalog refusal.
These tests have not been executed locally and source presence is not acceptance.

Migration-owned app_user privileges are SELECT/INSERT/UPDATE on topup_intents,
SELECT/INSERT on event_store, SELECT on subscribers and invoices plus UPDATE(id)
on each to permit the existing locking SELECTs, and SELECT on
integration_capability_bindings, integration_installations and domain_settings.
Released Payments owns module schema usage and DML grants. UPDATE(id) is real
identity DML: PK/FKs are not a universal identity immutability fence, especially
for unreferenced rows. These narrow temporary grants require security review
before activation and must be revoked with receivables caller transfer/global
legacy DML fencing. They do not authorize financial-column or subscriber-profile
updates. No generic GRANT ALL or test-only privilege repair is supplied.
