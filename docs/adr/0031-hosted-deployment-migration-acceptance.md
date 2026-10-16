# Hosted deployment migration acceptance

Status: implementation; real hosted run and staging rehearsal pending.
Owner: deployment control adapters (`production_deploy_migration`,
`production_deploy_probe`, `production_trusted_deploy`); schema floors remain
owned by their existing read-only verifiers, and Alembic owns schema changes.

The explicitly dispatched `Build release candidate once` workflow runs
`deployment-migration-acceptance` after `build`. This job consumes the build's
exact source revision, tree and immutable image digest. Ordinary source CI
never depends on this post-build job, avoiding a candidate-build/CI cycle.
The existing staging workflow requires success of the entire candidate run;
a failed real migration acceptance job therefore prevents staging admission.
The acceptance job has only actions, contents and packages read permissions.

The reviewed genuine predecessor is recorded in
`scripts/ci/fixtures/deployment_migration_predecessor.json`: source
`b0bc01a656fbaa6023ffe0f0e97189b4e92ca4c8`, tree
`10822ff0033f8e818e3ce39d3777fe32818ac987`, image
`sha256:e0eb5277e78c228cf01d7476506e56eb475a9cab150db2f259763c58255adeae`.
Its production health remains **UNVERIFIED**. Published build evidence, the
actual workflow run, Git source/tree, image RepoDigests and provenance labels
must agree. Embedded migration/provider source bytes and privileged bootstrap
owner bytes must match their exact checkout. An unavailable or expired artifact
fails closed; labels alone never authorize a baseline substitution.

The harness creates a private internal bridge, real PostGIS 16, Redis and a
named PostgreSQL volume on a disposable GitHub-hosted runner. It bootstraps the
database with the actual predecessor's existing privileged bootstrap owners,
then runs that predecessor's Alembic chain to its real heads. It requires absence
of both 691 correlation columns. Every serial scenario clones this migrated
baseline. Current-source fresh schema stopped at 690 cannot replace it: the
squashed base can already include current ORM columns. No stamp, fabricated
version rows, metadata schema substitution, or synthetic migration is used.

PostgreSQL's superuser only manages the disposable cluster, its fixture locks,
and privileged bootstrap. The existing bootstrap creates the canonical
`dotmac_app`/`app_admin`/`app_user` roles and their grants; the harness does not
invent replacement privileges. It sets runner-private generated authentication,
preinstalls only the migration-declared PostGIS/topology/pg_trgm/btree_gist
extensions, and verifies the restricted role postures and existing bootstrap
contracts. Baseline Alembic, candidate Alembic, captured runtime connections,
and database floor readers use `dotmac_app`, the restricted migration login
named by the production runbook. Permission failures fail closed. There is no
superuser migration fallback or permanent database CREATE grant.

Captured incumbents and a captured writer use the exact predecessor digest.
Their long-lived idle command establishes real Docker identity and topology;
it does not claim application startup, worker-drain behavior or health proof.
The harness resolves the checked-in Compose configuration with a private
fixture environment and retains the supported observability ExtraHosts. It
captures running identities, stops those exact IDs and invokes the real
migration controller with actual observed and dependency-resolved heads.

Fault injection is confined to `deployment_migration_fault_child`. Each lost
CREATE/START/REMOVE acknowledgement follows the real successful Docker call.
Each crash occurs after the real journal owner has durably written its phase.
Inspect, endpoint capture, authorization verification, schema readers, Alembic,
classification and reconciliation are never replaced. A requested durable
crash that was not reached is a failure. A real PostgreSQL ACCESS EXCLUSIVE
lock on `topup_intents` triggers 691's actual five-second lock timeout; a
one-second controller budget also proves a running runner is retained.
Email638 is already installed at the predecessor, so nonzero migration cannot
claim SAFE_PREDDL. The required disposition is FORWARD_REPAIR_REQUIRED.
Lock lifetime is bounded independently of cold image imports; release targets
the unique observed backend PID, start time, application name, database and live
granted lock. Each acknowledgement fault must produce a public fired-event
record. Pre-start crashes remain unresolved without starting a runner; retained
exited runners must reconcile COMPLETE with unchanged exact terminal evidence.

Coverage includes successful upgrade, actual installed floor/effective-head
classification, retained running and exited runners, acknowledgement loss,
durable journal phase crashes, stale invocation/hash refusals, changed writer
inventory, restarted writer, database/broker endpoint identity, network and
critical-host mapping drift, unknown installed schema, malformed maintenance
FDs, and backward repair refusal. SAME digest maintenance calls the real
trusted admission owner with a stopped incumbent and passes its actual
unlinked read-only FD7 and canonical FD8 to a child. Supervisor TERM/INT/HUP
uses the actual child supervisor and inherited canonical lock descriptor.

The known-predecessor recovery scenario holds the actual 691 lock, crashes the
controller after durable EXITED evidence, and reconciles the retained nonzero
runner to FORWARD_REPAIR_REQUIRED. Fresh admission must derive
`MaintenanceForwardRepairBasis.UNCHANGED_KNOWN_PREDECESSOR` and return the
failed invocation's unchanged start heads. This branch requires known installed
Email638 and known Payments predecessor observations. A distinct private LOCAL
authorization fixture binds the SAME exact candidate digest. FD7/FD8 preparation
and the real migration controller then create a new invocation UUID/container,
archive the failed journal unchanged, and migrate the real predecessor schema to
COMPLETE. The old exited runner's terminal evidence must remain unchanged; it
is never restarted. The installed-at-candidate SAME scenario remains separate.

Synthetic authorization documents are private **LOCAL parser/control
fixtures**, never production approval. They are destroyed with fixture state,
never uploaded or embedded in the report. This does not execute the full
`deploy.sh` wrapper, whose staging/production identity restrictions remain
intact. No bypass, fake production marker, private configuration or protected
credential is introduced. Reports contain only public image identities,
heads, journal phases, invocation/container IDs, fault points, scenario results
and fixture cleanup observations. Cleanup targets only recorded fixture IDs,
volumes and network; final Docker inventories must prove their absence. There
is no broad prune operation.
Runner cleanup enrollment first verifies its immutable journal identity and
actual configuration; lifetime-owned IDs remain in the final absence check even
after ordinary removal. Pending journals are inspected on controller exceptions
so retained runners can be cleaned by exact identity.

Two positive claims remain explicitly unproved without additional genuine
artifacts: SAFE_PREDDL requires an image before both Email638 and Payments691;
FORWARD maintenance requires a second actual descendant image. Full wrapper,
backup, startup, health, and restoration proof still requires an explicitly
authorized production-shaped staging rehearsal. Source-only checks and the
hosted component lane do not replace that operational gate.

Before source merge, require the repository-prescribed hosted checks on the
exact head and base. After the separately authorized candidate build publishes
its immutable image, this post-build component lane must pass before the
candidate workflow can admit staging. Complete cutover acceptance additionally
requires the unproved artifact cases and authorized production-shaped staging
rehearsal; component success is not cutover approval. Development on the
workstation performs only pinned Ruff 0.15.1 and AST static checks for this
slice; it does not run Docker, PostgreSQL, pytest, migration code, SSH or
dependency installation.
