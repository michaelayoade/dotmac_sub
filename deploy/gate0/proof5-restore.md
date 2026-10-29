# Gate0 proof5 controlled recovery draft

Status (2026-10-22): complete source draft under the isolated measurement-only
exception; unpublished, unintegrated, and **not PostgreSQL acceptance**. Shipping
caps remain unchanged. No host, Docker daemon, credential store, or database was
contacted for this draft. The live catalog-locking contract below remains an
explicit prerequisite to operational use.

`scripts/db_backup.sh` remains the sole capture owner and
`scripts/restore_drill.py` the sole restore owner. `restore_capture.py` adds the
controlled interval and private evidence binding. `proof5_restore.py` consumes
that protected bundle on the enrolled dedicated test host. There is no second
backup/restore engine. Existing metadata inventory owns comparison of roles,
schema/table ownership, RLS ENABLE/FORCE, policy identities, Alembic heads and
complete effective privilege matrices, with only fixed divergence categories
in public output. The database-property limitation in `docs/RECOVERY_BUNDLE.md`
remains; this adapter adds no password, database-property or membership parity
acceptance claim.

## Enrollment and private transport

A human must independently enroll root-protected `/etc/dotmac/gate0-host.json`
with exactly `purpose`, `hostname`, `machine_sha256`, `docker_sha256`. Capture is
restricted to `capture` / `hp-server`; restore to `restore` / `vmi3537655`
(dedicated server `85.190.246.211`). Identity compares the actual hostname and
machine-id hash, requires root, and pins `/usr/bin/docker` bytes. Writable or
symlink inputs/parents and alternate Docker environment endpoints are refused.
Docker always uses the explicit local Unix socket with a scrubbed environment.
The configuration contains public identity hashes, never credentials. Root
control remains privileged; this is an enrollment boundary, not containment
against an already compromised root principal.

Capture's parent must already exist, root-owned and mode0700. A fresh mode0700
workdir isolates retention from all existing backups. Only the existing backup
owner reads its documented, protected deployment `.env` at runtime; adapters do
not source or print it. Owner output is discarded. The runtime Docker wrapper
pins the executable/socket and clears inherited environment. Transfer the
whole capture directory through the approved human-held private channel;
preserve root ownership/private modes and verify the protected capture digest
against the approved source observation. Never upload dumps, globals, source
inventory, `.env`, credentials or customer rows to Git, public evidence or chat.

## Protected campaign and capture plan

Both CLIs require `--plan <root-protected-json>`. `CapturePlan` strictly decodes
one closed typed document. Its `context` is exactly the shared `GateContext`:
run_id, phase, prior_run_id, source_revision, source_tree, deployment_ref,
tool_revision, host_key_id, action_refs and deadline. All public times use
canonical UTC `Z`. Additional fields are observation_not_before, source_identity,
test_host_identity, owner_sha256, library_sha256, source_permit_sha256,
restore_permit_sha256, capture_tool_sha256, restore_tool_sha256, image and
capture_sha256. Hash fields use `sha256:` plus 64 lowercase hex characters.
This is an installed protected plan, never an untrusted CLI boolean or a
self-approved capture assertion.

The source plan has capture_sha256=null. Before any Docker work, capture checks
the independently reviewed backup owner/library byte pins, source enrollment
permit pin, capture/restore adapter pins, actual clean source Git HEAD/tree and
actual clean tool checkout revision against that plan. It records those actual
identities, full context and byte hashes in capture.json, and repeats source and
tool checks before closing the capture. Dirty checkouts, changed sources and
unreviewed backup tools refuse. The existing backup manifest must name
`dotmac_sub`, the observed source revision and a real UTC creation time inside
the enforced interval (allowing only its existing whole-second timestamp
rounding). Capture time must lie inside the protected campaign window and
cannot be future-dated.

After governed review and private transfer, install a protected restore plan
with that same context/pins and capture_sha256 set to the **independently
approved exact capture.json bytes hash**. Restore checks its enrolled permit
and current tool revision/bytes, the approved capture hash, record context and
all recorded pins, source HEAD/tree, bundle database/revision/time and inventory
hashes before invoking the restore owner. The image must match the plan's exact
immutable image. Current execution and the recorded capture both must fall
inside the approved observation/deadline window.

The main harness campaign context must exactly equal plan.context. CP's
protected CampaignScope.context must equal that same context, and its
restore_capture_sha256 must equal plan.capture_sha256; its separately approved
bundle/inventory/source/test-host pins remain mandatory. This exact approved
capture hash transitively binds the private plan and observed source context;
the public RestoreEvidence schema is unchanged. Installing a new plan or
approving a different capture remains a governed human operation, not something
the source adapter silently performs.

## Observed noDDL contract

A persistent privileged `psql -X` transaction derives **every heap/partitioned
pg_catalog relation**, including shared `pg_authid`, `pg_auth_members` and
`pg_database`, acquires SHARE locks in OID order, and SHARE-locks
`public.alembic_version` to block migration-head DML. SHARE conflicts with the
RowExclusive locks required for catalog writes while permitting dump SELECTs.
The adapter observes its backend PID and the exact required versus granted
ShareLock relation sets. Missing, duplicated or extra locks fail closed.

The observed interval starts after successful lock observation, covers globals
FIRST, database dump, hash validation and source metadata collection, and ends
after a second identical observation on the same live backend. This binds role,
grant, ownership, RLS/policy, schema and migration metadata across separately
captured artifacts. It does not rely on a caller boolean, timestamps alone,
or a cooperative advisory lock. Lock acquisition waits at most five seconds;
queries use nonblocking pipe reads with an absolute forty-second deadline and
a 64MiB record bound; partial lines, oversized records and EOF refuse. Buffered
complete subsequent records are preserved across queries. Owner execution waits
at most thirty minutes.
Every exception rolls back/closes the holder, killing/waiting if necessary.
A failed workdir stays private for governed diagnosis, has no valid capture
record, and cannot be consumed as proof5. No cleanup deletes existing backups.

**Unproved:** PostgreSQL 16's actual support and writer lock behavior for this
catalog set, including shared catalog updates, must be demonstrated using the
following packet. SQL refusal, inability to prove a blocked writer, or a gap in
coverage means stop and retain the live gate. Do not weaken the freeze or
substitute an assertion to obtain acceptance.

## Dedicated PostgreSQL acceptance packet (requires separate host authorization)

1. Use an exact source/tool checkout on the enrolled dedicated server and
   PostgreSQL16 image pinned by immutable digest. Create a fresh disposable
   database through the real Alembic migration chain at that exact revision;
   retain the repository's approved backup/role bootstrap owner. No local
   Docker, SQLite or metadata.create_all acceptance lane is permitted.
2. Exercise the persistent freeze on that migrated disposable source. In
   independent sessions set a short `lock_timeout` and attempt CREATE/ALTER
   ROLE, GRANT/REVOKE membership and table/schema privilege, ALTER OWNER,
   ENABLE/DISABLE/FORCE RLS, CREATE/ALTER/DROP POLICY, CREATE/ALTER/DROP schema
   objects, and Alembic head UPDATE. Include role operations connected through
   another database to exercise shared catalogs. Every mutation must time out
   while the holder is live; verify no state changed. Dump/inventory SELECTs
   must still finish. Record exact required/granted locks before and after.
3. Kill/disconnect the holder during globals, during dump and before inventory;
   simulate lock timeout, owner timeout/failure and malformed lock output.
   Every capture must fail with no admissible capture record. Verify the
   control backend and its locks disappear after success and every failure.
4. Only after that packet passes, separately authorize Seabone capture on
   `hp-server`, container `dotmac_sub_db`, database `dotmac_sub` (existing DB
   alias `db`). Enroll source identity and use a root-only private parent.
   Run `python -m deploy.gate0.restore_capture --root <protected-checkout>
   --private-parent <root-only-parent> --plan <protected-source-plan>` under the governed window. Never run
   the fault/mutation tests on that source.
5. Privately transfer that exact approved bundle to `vmi3537655`. Run
   `python -m deploy.gate0.proof5_restore --capture <private-directory>
   --image <postgres-image@sha256:digest> --plan <protected-restore-plan>`. Verify exact metadata parity and
   scratch absence. Repeat real failure cases for startup, malformed dump,
   inventory mismatch and cleanup failure; none may produce pass. Independently
   observe no owned scratch container remains; never remove foreign resources.
6. The root-only, exclusive-create `proof5-outcome.json` contains only the
   closed public contract, inventory digests and capture identity/interval.
   After an admitted capture, a restore exception preserves known source/capture
   facts, records unknown restored digest/parity/scratch observations as null, and
   produces terminal failure with cleanup unproved; no all-zero digest or
   fabricated absence is emitted. An ordinary result lacking restored inventory
   carries a null digest. Its pass requires the typed restore owner's real comparison, matching
   source/restored digest and observed cleanup. Send only the hash-pinned
   public outcome through the main proof harness's protected campaign transport.
   CP append/push receipt and authoritative identical replay readback remain
   mandatory. This module never prints `GATE0 PROOF 5: PASS` on its own.

Offline verification covers strict paths/duplicates/sizes, enrollment before
processes/private reads, pinned image refusal, SQL-control failure, lost session,
rollback/kill/wait, failed owner capture, hash corruption, exact parity/digest and
cleanup requirements, PostgreSQL canonical true/false role decoding, protected
provenance mismatches, and sanitized output/DEFLATE failures. Existing cleanup and metadata comparison regressions stay
in the focused set. These tests establish adapter behavior, not PostgreSQL lock,
restore or migrated-schema acceptance. Full prescribed CI, independent source
review, live proof1–5, signoff and same-session repeatability proof2–4 remain separate gates.
