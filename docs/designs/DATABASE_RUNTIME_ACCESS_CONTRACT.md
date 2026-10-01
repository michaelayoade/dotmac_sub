# Permanent Sub database access contract

Status: target direction approved on 2026-10-01; object-operation policy and
live execution pending review. This document grants no privileges. Database
authority belongs to Sub's deployment/database owner; each business service
continues to own its decisions and transitions.

The cutover is forward-only. The application and worker authenticate as
`app_user`; migrations authenticate as `app_admin`. No compatibility runtime,
superuser runtime fallback, or retired role membership is part of the target.
The pure compiler in `app/database_role_plan.py` requires one explicit policy
entry for every observed object. There is no default grant for an unclassified
object and no `GRANT ... ON ALL TABLES` operation.

## Operation decisions to put in the reviewed object policy

For a mutable business table, recommend only the `SELECT`, `INSERT`, `UPDATE`
and `DELETE` operations its named owning service needs. Approve these per
object after checking the writer and lifecycle; a model definition is not
evidence of a write operation. Do not grant `TRUNCATE`, `REFERENCES`, `TRIGGER`,
grant options, database CREATE, or object ownership to the application role.
Retired tables and platform-plane tables receive no application grant,
including column-level rights. Tables with immutable facts are separate from
mutable claim, settlement and retention ledgers.

### Source-backed exceptions

| Object(s) | Proposed `app_user` operations | Source of the operation contract |
| --- | --- | --- |
| `public.tenants` | SELECT, INSERT | `app/services/operator_tenant.py`: idempotent creation and lookup; update/delete forbidden. |
| `public.subscriber_status_history` | SELECT | `app/services/funded_inactive_exposure.py`: scheduled audit reader; no current writer found. |
| `public.idempotency_records`, `public.outbox_events` | Published SELECT, INSERT, UPDATE, DELETE | Migrations 556/557 and relay claim/settlement; these are mutable ledgers. A different retention/provider contract needs its own change. |
| `public.platform_idempotency_records`, `public.platform_outbox_events` | None | Migrations 556/557 explicitly revoke all tenant-role table and column privileges. Platform and dispatcher identities retain their separately reviewed plane contracts. |
| `public.machine_credentials` | Runtime SELECT; issuance INSERT; rotation/revocation UPDATE requires a named execution role; no DELETE need established | Sub authentication and issuance, pinned Kernel a97 `machine_auth` and `machine_rotation`. Migration 551's former CRUD grant is evidence, not approval of the permanent list. See the schema/attribution gate below. |
| `public.billing_receivable_projection_version_seq` | USAGE | `app/services/billing/receivable_projection.py`: `nextval`. |
| `public.alembic_version` | None | Migration bookkeeping belongs to `app_admin`. |
| Eight retired `public.vas_*` tables | None | `docs/designs/VAS_RETIREMENT.md`. |
| `public.splynx_archived_ticket_messages`, `public.splynx_archived_tickets`, `public.splynx_id_mappings` | None | Migration 330 retires runtime models/writers. |
| `public.payment_prepaid_applications_archive` | None | `scripts/migration/payment_prepaid_application_archive_schema.py`: no runtime model/writer. |

The following 26 tables have checked-in unconditional SQL UPDATE/DELETE
vetoes. Their normal runtime proposal is SELECT/INSERT, subject to checking
the actual installed guards and named writer before approval:

- `withholding_tax_transitions`
- `installation_project_lifecycle_events`
- `as_built_route_review_events`
- `lead_origin_captures`
- `customer_experience_handoff_events`
- `provisioning_readiness_decisions`
- `provisioning_readiness_checks`
- `prepaid_coverage_reconciliation_runs`
- `prepaid_coverage_reconciliation_items`
- `subscription_billing_grants`
- `proposed_route_revision_review_events`
- `subscription_lifecycle_events`
- `inbox_routing_events`
- `inbox_status_transition_events`
- `inbox_agent_presence_events`
- `inbox_audit_reconstruction_runs`
- `quote_discount_history`
- `sla_period_score_revisions`
- `sla_score_eligibility_intervals`
- `sla_score_monitoring_intervals`
- `invoice_discount_history`
- `carried_source_identity_adjudications`
- `paystack_outside_window_recovery_runs`
- `inbox_customer_completion_policy_versions`
- `customer_subledger_opening_corrections`
- `native_prepaid_opening_repairs`

`quote_payment_reviews` additionally has ORM UPDATE/DELETE vetoes in
`app/models/payment_proof.py`; these do not prove a SQL privilege boundary.
Check its installed enforcement before approving the same SELECT/INSERT
proposal. Conditional guards on other tables are not unconditional
append-only contracts. Any exceptional repair gets a separately named owner,
operation and approval; it does not widen the normal application grant.

Module tables follow their published per-table grants and selected persistence
plane. The observed 52 module tables have 35 CRUD grants, 15 SELECT/INSERT
grants and two SELECT/INSERT/UPDATE grants. None grants TRUNCATE. These are
observations to reconcile with the exact installed package, not a policy
derived from table names. The observed `mod_billing.billing_accounts.id`
column UPDATE grant also needs an explicit column policy.

## Remaining classification and compatibility gates

The 2026-10-01 read-only staging catalog contains 1,039 objects and 10,163
relation columns; 626 of 636 modeled public tables lack application privileges.
This identifies the review work, not a reason to copy the former superuser's
rights. Every table, sequence, enum/domain, routine and schema must be covered
by the exact policy; source owners, PUBLIC grants and column grants are
reviewed explicitly. The observation is not a ready-to-execute plan.

Michael approved the proposed policy for `ont_bundle_assignments`,
`service_order_actions`, `support_ticket_actor_assignees`, `tenant_domains`
and `erp_operational_sync_state_id_seq` on 2026-10-01: retain their data under
`app_admin` with no `app_user` grants. No current runtime caller was established
in the bounded source review. The zero-runtime-access decision is explicit;
it is not inferred from missing search hits and does not declare these objects
retired or authorize dropping them. Their historical provenance and future
domain ownership remain unresolved. Put this policy in the exact object plan;
ownership transfer still needs the separate reviewed execution gate below.

Kernel a97 maps four machine-credential columns absent from Sub migration 551
and the staging catalog: `source_application`, `next_key_hash`,
`rotation_started_at`, `rotated_at`. An isolated a94 installation does not map
them. The forward deployment therefore needs schema and issuance alignment
before machine authentication can be accepted. Existing digest rows cannot
identify their holder: attribution is an owner's decision, never a guessed
backfill. An unattributed credential remains refused by the published kernel.

## Ordered authority boundary

1. Finalize the named object-operation policy against a fresh complete
   catalog and exact candidate packages; review excess direct, column,
   effective and default ACLs and grant options.
2. Prepare a separately reviewed cluster-role operation retiring
   `dotmac_app -> app_admin` and
   `dotmac_schema_bootstrap -> dotmac_app`, and installing the declared
   constrained `dotmac_schema_bootstrap -> app_admin` membership. Inventory
   affected configured consumers across the cluster; an empty session list
   does not prove that none exist.
3. Bind the ordered ownership/grant operation and retirement operation to
   their catalog and policy digests. Keep database ownership with the
   separately reviewed infrastructure principal: `app_admin` must not gain
   database CREATE. The object compiler cannot execute or approve either
   operation and emits no membership or revoke statements.
4. Obtain a restorable backup, rehearse the exact operation on disposable
   PostgreSQL, prove the permanent runtime's positive/negative access cases,
   then obtain execution authority for the exact maintenance boundary.
5. Install the distinct approved held credentials, switch app/worker to
   `app_user`, and observe actual identities and periodic pending recovery
   against the accepted staging digest. Retired authority stays retired;
   repair forward if a required operation was omitted.

The production host remains a separate authorization. No role, grant,
membership, credential or runtime change is authorized by this document.

## Proposed credential provisioning

Use environment-specific OpenBao records for the three authorities. The
following are **proposed new pointers**, not observed or approved existing
records:

| Authority | Proposed OpenBao pointer | Consumer |
| --- | --- | --- |
| Runtime | `secret/dotmac/sub/staging/database/app-user#url` | Sub app and worker only. |
| Migration | `secret/dotmac/sub/staging/database/app-admin#url` | One-shot migration/verification commands only. |
| Schema bootstrap | `secret/dotmac/sub/staging/database/schema-bootstrap#url` | Explicit constrained prerequisite repair only. |

Check the existing secret inventory before provisioning; reuse an approved
matching record if one already exists. Provision independent passwords for
the named roles without changing the shared `postgres` credential. Record
only paths and materialization evidence in the operation plan.

The deployment owner should materialize the migration URL into an absolute
file outside the checkout, deployment directory and current working directory
(proposed `/run/dotmac/sub-staging/migration.url`). The held-file loader requires
a regular non-symlink file owned by the executing deployment UID with mode
0400; its parent should be private to that UID. A root-only file unreadable
by the deployment UID would not satisfy this contract. Set the protected
environment's `MIGRATION_DATABASE_URL_FILE` to the approved file pointer.
The loader passes the value only through the one-shot child's environment;
long-running Compose services mask it.

The existing deployment contract reads the runtime URL from its protected
deployment `.env`. Updating that file is a separate reviewed cutover step;
it must contain only the `app_user` runtime connection, never the migration
credential. Verify both actual connections against the same database/backend
before any ownership change, and verify the app and worker consume the runtime
value after recreation. Materialization uses the approved secure OpenBao
access path; no token or credential is transmitted over unprotected HTTP.

These pointer and file recommendations do not provision records, rotate
credentials, or authorize secret access. Their exact paths and consumers
must be included in the reviewed maintenance operation.
