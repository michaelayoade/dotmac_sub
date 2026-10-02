# Machine credential alignment with Kernel 0.1.0a97

Sub owns `public.machine_credentials` in revision `551_machine_credentials`.
Kernel owns the ORM and machine authentication contract, but its Alembic
lineage cannot be composed into Sub's public lineage because it also changes
Sub-owned tables. Sub revision `639_machine_attribution` mirrors the
machine table portion of Kernel's published `0028_machine_attribution` revision
after Sub revision `638_payment_email_cutover`. It adds nullable
`source_application`, `next_key_hash`, `rotation_started_at`, and `rotated_at`
with the matching index, scoped uniqueness, and checks. It does not alter
Sub's separate `audit_events` table or its local audit writer.

Existing credential rows keep `source_application = NULL`. A digest cannot
identify the calling application, so no attribution is inferred or backfilled.
Kernel a97 refuses those rows during authentication. An owner must establish
the actual caller before activation; a later contract migration can require
non-null storage after that work is complete. The dedicated
`machine_credential_hmac_key` stays a held, product-sourced secret.

The standalone issuer requires `--source-application` to name a code in
`ACCEPTED_SOURCE_APPLICATIONS`, a comma-separated deployment configuration
whose default is empty. This is an explicit peer list, not a source of new
identities. Issuance uses Kernel's `issue_credential`, retains Sub's
non-empty-scope policy and label reuse refusal, and shows the raw key once
only after the transaction commits. It always uses Sub's named operator
tenant; the former `--tenant-slug` switch is removed because Sub's session
hook scopes every transaction to that one tenant. The secret's approved pointer is
`bao://secret/settings/machine_auth#machine_credential_hmac_key`.

Local validation covers PostgreSQL migration SQL and the installed Kernel a97
ORM/authentication path on SQLite. The PostgreSQL canary uses `SET LOCAL ROLE
app_user` to prove RLS enforcement; this does not prove an independent runtime
login. A separate `tests.integration.machine_cli_probe` command requires an
explicitly marked disposable cluster, matches the live database, server IP,
port and postmaster start against the oracle, and requires a direct, non-bypass
`app_user` login before the issuer writes. It captures the one-time key only in
memory and emits counts. The oracle connection only reads the migrated schema;
the isolated cluster must already provide the operator row and runtime grants.
The child cleans up its own row after verification. Live execution remains a
gate owned by the primary operator. SQLite cannot prove PostgreSQL RLS isolation.
