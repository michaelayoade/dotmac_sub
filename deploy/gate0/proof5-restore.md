# Gate 0 proof 5: restore comparison gap

Status: **partial source; full proof unimplemented; no live proof recorded** (2026-10-21). This
document records the bounded follow-up needed before a proof5 adapter can
honestly report acceptance. No fail-only executable is shipped.

Partial source now exists in `scripts/restore_inventory.py`: one metadata SQL
query, typed strict decoding, complete catalog-derived effective schema/table
privilege matrices, and a metadata-only first-divergence comparator. The existing
`drill_bundle(..., expected_inventory=...)` owner optionally collects restored
metadata with that same query, compares before cleanup, and fails on collection
error or divergence. Schema/table ownership, RLS ENABLE/FORCE flags, policy
identities and public Alembic heads are compared as well as non-builtin roles.
Eight offline inventory/owner tests and eleven cleanup tests pass. SQL execution
against migrated PostgreSQL remains unproved; mocked results are not database
acceptance. There is no source capture command, snapshot/freeze binding, trusted
host enrollment/guard, CLI inventory option or Gate0 PASS adapter in this partial
slice. Its optional comparison does not establish staging provenance. Existing
drill PASS remains insufficient for proof5.

The approved agent design §6, “Proof 5”, requires a real staging backup
through the production backup path, globals-first scratch restore, source
comparison of isolation metadata, and scratch cleanup. The production path
already calls `scripts/db_backup.sh` in `scripts/deploy.sh:1676`.
Its behavior is outside this increment's change scope.

## Existing evidence and missing acceptance checks

| Requirement | Existing source | Remaining gap |
| --- | --- | --- |
| Real staging production-path bundle | `scripts/backup/recovery_bundle_lib.sh:119` emits bundle metadata; `scripts/restore_drill.py:132` loads manifests | The manifest does not establish staging provenance, approved capture, or source inventory binding. Artifact hashes prove consistency with a manifest, not staging origin. |
| Roles and attributes match source | Optional inventory comparison covers non-builtin role login/bypass/superuser attributes alongside existing role contracts | Trusted source capture and real PostgreSQL comparison remain unproved. |
| Alembic heads match source | Optional inventory compares public Alembic heads | Source capture binding and real query execution remain unproved. |
| RLS-enabled table and policy counts match source | Optional inventory compares per-table RLS ENABLE/FORCE and policy identities, retaining existing isolation assertions | Trusted capture and real source/restore parity remain unproved. |
| Bidirectional schema/table privilege spot matrix matches source | Optional inventory covers effective schema/table privileges across the catalog-derived role/object matrix, including allowed and denied observations | Source capture, actual SQL execution and real parity remain unproved. |
| Ownership not collapsed | Existing collapse assertion plus optional schema/table owner comparison | Trusted source ownership capture and real parity remain unproved. |
| Cleanup completed before acceptance | `scripts/restore_drill.py` scratch lifecycle now labels each run, cleans startup/readiness failures, refuses unowned resources, checks removal and independently observes absence | Eleven offline mocked lifecycle tests pass. Dedicated-host execution and real scratch absence evidence remain unproved. |
| Dedicated test server enforced before subprocesses | `scripts/restore_drill.py:176` executes subprocesses; `:194` starts Docker | Generated scratch names and network isolation do not enforce execution host. There is no dedicated-host guard. |
| Metadata-only diagnostics | Dump-restore errors and optional inventory errors are sanitized; existing `ScratchPostgres.scalar()` can still include raw query stderr outside that path | A Gate0 adapter must sanitize every remaining infrastructure path before forwarding evidence. |

The present drill's `PASS` is therefore insufficient for Gate0 proof5.
`--self-test` (`scripts/restore_drill.py:513`), `--dry-run` (`:572`), fixtures,
and mocked subprocess results cannot substitute for the live proof. The CLI
also exits zero for `DRY_RUN_OK` (`:671`), so exit status alone is insufficient.

## Proposed minimal inventory contract (not an accepted schema)

Keep the existing recovery-bundle format and producer unchanged. Add a
separately versioned, typed metadata sidecar owned by the restore-proof
implementation. Suggested schema identifier:
`dotmac_sub.gate0_restore_inventory.v1`. Reject unknown versions, missing or
duplicate entries, unsupported probe kinds, invalid values, and empty required
probe sets. Suggested fields:

| Field | Type and meaning |
| --- | --- |
| `schema` | Exact version identifier above |
| `bundle_manifest_sha256` | SHA-256 of the exact approved recovery manifest |
| `source` | Explicit staging host identifier, database name, capture UTC time, deployed source revision, and backup-path identifier `scripts/db_backup.sh` |
| `capture_evidence_ref` | Reference to approved capture evidence binding this staging source and backup; no credentials or customer payloads |
| `roles` | Sorted records `{name, can_login, bypass_rls, superuser}` for the declared non-builtin source role set; boolean attributes |
| `alembic_heads` | Sorted unique revision identifiers from source `alembic_version` |
| `rls_enabled_table_count` | Nonnegative integer, using the same declared schema/table scope on source and restore |
| `policy_count` | Nonnegative integer, using that scope on source and restore |
| `privilege_probes` | Sorted records `{role, object_kind, schema, object_name, privilege, allowed}`; kinds `schema` or `table`, schema object name null, allowed boolean |
| `table_owners` | Sorted records `{schema, table, owner}` in the same declared application-table scope |

Capture role, schema, privilege, and migration metadata in a controlled backup
window with no concurrent role/schema changes; establish and record how the
inventory corresponds to the database dump's snapshot. Matching timestamps
alone do not prove consistency. Source capture is observational; do not add
an equality gate over unrelated external state.

The probe set must explicitly cover application-to-platform and
platform-to-application permissions, including the designated denied schema
and table privileges. Derive exact role/object coverage from the owning
isolation contracts. Do not infer approval or matrix
completeness from an arbitrary operator-supplied JSON list. This document
does not add password, role-membership, or database-property acceptance gates.
The existing database-property capture limitation remains documented in
`docs/RECOVERY_BUNDLE.md`.

## Required implementation and live evidence

Implement a thin typed proof adapter over the existing restore owner, with
source inventory collection/comparison and verified cleanup in that owner.
Before any operational subprocess, establish the authorized dedicated test
host and reject local/production execution and Docker endpoint redirection.
The host identity mechanism needs enrolled, independently observed host evidence;
an environment declaration alone is insufficient. Never accept arbitrary restore commands or target
containers from a manifest.

After explicit authorization naming the host, run the bounded scratch restore
on dedicated test server `85.190.246.211`, using a fresh exact-revision checkout
and environment. Preserve network-none scratch isolation, add bounded resources
appropriate to the approved backup, and clean only this run's resources.
Successful cleanup must be observed even after startup, restore, assertion,
or timeout failures. Do not use local Docker or shared host databases.

Acceptance needs the real staging capture and approved manifest binding,
source and restored metadata comparisons above, safe first-divergence metadata,
and scratch absence evidence. Credentials, dumps, and customer data rows do
not belong in the proof output or Git. Emit exactly one terminal
`GATE0 PROOF 5: PASS` or `GATE0 PROOF 5: FAIL`, with nonzero exit on refusal,
missing comparisons, mismatch, infrastructure failure, or failed cleanup.
Only the complete real run can emit PASS.

Planning estimate: 300–500 production Python/shell lines and 200–350 focused
test lines, roughly 1–2 engineering days plus review and the approved live
window. This includes strict typed input parsing, safe output, host refusal,
metadata collection/comparison, failure cleanup, and refusal/mismatch tests;
it is an estimate, not permission to expand this source increment. Offline
stub tests can verify refusal and cleanup plumbing; actual restore evidence
still requires the named server and real staging bundle.

Knowledge consulted with scope `project:dotmac_sub`: canonical records
`dotmac-dedicated-test-server` (v8) and
`observer-test-evidence-requires-a-fresh-install-per-commit` (v3).
No hosts contacted, credentials retrieved, tests run, or Docker used for this
documentation-only inspection. Current runtime remains unchanged.
