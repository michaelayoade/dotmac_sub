# Database authority plan compiler

`scripts/plan_database_authority.py --catalog <path> --policy <path>` reads two
bounded JSON files and emits JSON. It has no database connection, SQL executor,
role mutation, or approval flag. A `ready_for_review` result is a proposal for
human and database review, not authority to execute it. A blocked result has no
SQL statements. The SHA-256 values bind the exact database, catalog, policy,
target role, and ordered statements; they are comparison evidence, not an
authorization token.

## Inputs and evidence

The catalog JSON decodes to `CatalogSnapshot` in `app/database_role_plan.py`.
It names `schema_version: 1`, the exact `database` and `database_owner`, the
complete `scope_schemas`, role postures, memberships, direct
`database_acl_grants`, default ACLs, and every in-scope object. Role posture
includes `inherits` (`rolinherit`) and effective database `CREATE`.
Membership evidence includes `inherits`, `set_option`, and `admin_option`.
Each object has its kind, schema/name/subkind, owner, `extension_owned`,
routine argument types, raw `acl_grants`, `column_grants`,
effective privileges for the protected roles and PUBLIC, and `columns` for
relations. Every `Grant` and `ColumnGrant` records both `privileges` and the
subset held `WITH GRANT OPTION` as `grant_options`. This is direct catalog
evidence; effective grants use an empty `grant_options` tuple. An ACL report of
four table DML counts is insufficient.

All ten evidence flags must be true: `objects_complete`,
`relation_columns_complete`, `acl_complete`, `grant_options_complete`,
`column_acl_complete`, `effective_privileges_complete`,
`membership_complete`, `default_acl_complete`,
`database_privileges_complete`, and `database_acl_complete`. The collector
must enumerate raw database ACL entries, including `WITH GRANT OPTION`, and
actual relation column names from `pg_attribute`, excluding dropped columns.
It cannot infer a complete column list from columns that happen to carry ACLs.
An omitted field or a false flag blocks the plan. The catalog collector
supplies facts only; it does not classify objects or approve grants.

The separate policy JSON decodes to `AuthorityPolicy`. It names the same
database and scope schemas, `expected_database_owner`, `target_owner:
app_admin`, `runtime_role: app_user`, exact `approved_source_owners`, and one
`ObjectPolicy` per in-scope non-extension object. `classification: none` means
no named table/object or column grants; `classification: named` requires at
least one explicit grant. Each object grant names a role, privileges, and
optional justification. Each column grant additionally names a real observed
column. PUBLIC grants require a justification. The policy also declares
`source_admin_principals` with a named principal and nonempty justification.
For effective-rights comparison, an explicitly justified PUBLIC object grant
also permits that same privilege in known roles' effective observations; it
does not approve direct grants to those roles or any privilege absent from
the PUBLIC or named-role policy.
Only the target migration principal or a catalog-proved superuser DBA may be
declared; `app_user`, `platform_api`, `PUBLIC`, and legacy `dotmac_app` are
excluded. This declaration accounts only for that principal's observed
**effective** object privileges on an object it does not own. It never
approves a desired grant, direct object or column ACL, or grant option.

`operational_bootstrap_memberships` must declare exactly
`dotmac_schema_bootstrap -> app_admin` with a nonempty justification. Catalog
evidence must show the bootstrap role as LOGIN, NOINHERIT, NOSUPERUSER,
NOBYPASSRLS, NOCREATEDB, and NOCREATEROLE; the observed membership must have
`inherits=false`, `set_option=true`, and `admin_option=false`. The complete
direct database ACL must grant `CREATE` to that named role without grant
option. PUBLIC or inherited `CREATE` cannot substitute. This declaration is
an expected-state comparison, never permission to grant membership, change
roles, or execute the plan. No membership or database-grant SQL is emitted.

Unknown objects, unexpected owners, unknown roles/privileges, unsafe role
posture, excess direct or effective rights, unreviewed column rights, other
memberships with a protected role on either side, and nonempty default ACLs
block the plan. Existing non-owner grant options
also block; column grant options require separate review. The compiler emits
no `REVOKE` or `WITH GRANT OPTION` SQL.

Every in-scope schema, including `public` when owned by
`pg_database_owner`, needs an explicitly approved source-owner transfer to
`app_admin`. This gives `app_admin` schema-owner `CREATE` without making it
database owner or granting database `CREATE`. Schema, type, relation, and
routine ownership and reviewed missing grants are ordered per object. Column
grant SQL only adds privileges absent from the exact observed column ACL and
only for catalog-proved columns. System and extension-owned objects are
excluded, and a policy entry for one is rejected.

The current observed `mod_billing.billing_accounts.id` column `UPDATE` ACL
for `app_user` is **catalog evidence**, not an approved policy decision. A
reviewer must classify it in that object's `column_grants` policy before a
plan can become `ready_for_review`. The repository does not ship a blanket
legacy grant policy or a plan executor.
