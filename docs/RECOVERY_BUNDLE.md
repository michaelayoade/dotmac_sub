# PostgreSQL recovery bundle (Sub)

A backup that cannot restore the **isolation model** is not a backup. Sub's
tenant isolation is FORCEd row-level security evaluated against application
roles, plus platform tables protected by `REVOKE` from those roles. A plain
`pg_dump -d` captures tables, policies and GRANTs but **never the roles they
name** — and with `--no-owner --no-privileges` it strips ownership and ACLs
too. This bundle is the fix; the defect record is
`docs/designs/SUB_DEPLOYMENT_EXECUTOR_CENSUS.md` ("The backup defect").

## Facility status

Starter ships the real facility — `PostgresRecoveryBundle.v1` in
`dotmac-deployment-foundation` (`recovery.py`: role closure, manifest,
restore ordering, identity-stripping refusal). It deliberately runs no
`pg_dump`, and Sub does not yet depend on that package (pinning is blocked on
the Foundation a2 release, per the census). What is checked in here is the
**minimal host-side capture aligned with that facility**, to be replaced by
the facility's own capture when the pin lands. Manifests therefore carry
`"schema": "dotmac_sub.recovery_bundle.v1"` plus a `facility_target` pointer —
claiming the facility schema before its 13-component closure is produced
would be false labelling.

## Bundle format

All dump flags come from one file: `scripts/backup/recovery_bundle_lib.sh`.
Both producers emit the same three artifacts under one stem:

| Artifact | Producer command | Contents |
|---|---|---|
| `<stem>.globals.sql.gz` | `pg_dumpall --globals-only --no-role-passwords` | roles, memberships, tablespaces, per-role/db config — **no password verifiers** |
| `<stem>.sql.gz` | `pg_dump -d <db>` (no `--no-owner`/`--no-privileges`) | schema + data **with** ownership and GRANT/REVOKE evidence |
| `<stem>.manifest.json` | `recovery_write_manifest` | sha256 + bytes per artifact, pg_dump version, UTC timestamp, git SHA, restore order |

Producers: `scripts/db_backup.sh` (on-box pre-migration bundle; dump filename
unchanged so deploy retention/resume keep working) and
`scripts/backup/backup_dotmac_sub_dbs_to_rclone.sh` (offsite; manifest
uploads last, retention counts bundles not files, MongoDB path untouched).

Invariants:

- **Globals first, fail-closed.** If the globals step fails, is empty, or
  contains password verifier material, the producer exits **3**, writes
  `<stem>.FAILED`, and leaves no dump behind to be mistaken for a backup.
- **Atomic.** Every artifact is written to a `.tmp.<pid>` sibling and renamed;
  the manifest lands last. A bundle without a manifest is incomplete.
- **No login material, ever.** `--globals-only` alone would export
  `SCRAM-SHA-256` verifiers into backup storage. Role passwords are installed
  at restore time from the approved secret source (pgpass discipline), never
  from a bundle.
- **Identity-stripping flags are refused** (`--no-owner`, `--no-privileges`,
  `--no-acl`, …) by `recovery_refuse_identity_stripping`, mirroring the
  facility's `IDENTITY_STRIPPING_ARGS`.

`--globals-only` was chosen over `--roles-only` so tablespace definitions and
database-level config/ACLs cannot drift into a separate, forgettable
artifact; the drill adjudicates the benign "already exists" noise by
asserting the restored model rather than by trusting exit codes.

## Restore order

1. Verify each artifact's sha256 against the manifest.
2. Apply **globals** to the fresh cluster (creates roles/memberships).
3. Install role login material from the approved secret source.
4. Create the target database, apply the **database dump** as superuser so
   `OWNER TO` and ACL statements land on the now-existing roles.
5. **Verify** with the restore drill before calling it recovered.

## Restore drill

`scripts/restore_drill.py` restores a bundle into a disposable scratch
PostgreSQL (`--network none`, tmpfs, generated `dotmac-restore-drill-*`
container, no published ports; production-resembling targets are refused)
and emits a JSON verdict asserting:

- manifest hashes match; globals carry no password verifiers;
- every role defined in globals exists after restore;
- contract roles hold their exact postures (sources:
  `app/commercial_module_prereqs.py`, `app/outbox_dispatcher_roles.py`;
  created by `scripts/bootstrap_commercial_module_prereqs.py` and
  `scripts/bootstrap_outbox_dispatcher_roles.py`, verified by migration
  `557_outbox_relay_prereq.py`);
- RLS-enabled tables exist and are FORCEd; no policies sit on RLS-disabled
  tables;
- `app_user` is denied `public.platform_outbox_events` (migration 557
  revocation);
- a foreign tenant sees zero rows in `public.outbox_events`;
- ownership is not collapsed onto the restoring superuser.

Modes: `--manifest <stem>.manifest.json` (full drill), `--dry-run`
(validate + plan, no docker), `--self-test` (synthetic fixture bundle built
with the production flags, then drilled — the CI mode; needs docker, no
production data).

Role-creation cross-reference (what globals now makes restorable):
`scripts/bootstrap_commercial_module_prereqs.py` (commercial/module roles and
schemas), `scripts/bootstrap_outbox_dispatcher_roles.py` (outbox dispatchers
and relay ownership), `alembic/versions/557_outbox_relay_prereq.py` (verifies
dispatcher roles; never creates them).
