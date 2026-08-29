# Build-once release artifact promotion

**Status:** Build-once candidate, digest-bound staging, production promotion,
production verification, backup enforcement, and publisher cutover implemented

**Amended 2026-08-28 (dual identity):** authorization and artifact are two
separate identities, and conflating them was a second dev-first assumption that
survived the branch migration:

- `authorization_main_revision` — the protected `main` tip whose checked-in
  workflow and verifier code performed the authorization. It says WHO
  authorized.
- `release_revision` — the exact staged candidate whose tree, digest, CI and
  staging acceptance are authorized. It says WHAT is deployed.

Under dev-first these were necessarily different commits on different branches,
so nothing distinguished "the code that authorizes" from "the code being
released". On a single trunk the promotion previously demanded
`release_sha == git rev-parse HEAD`, which is a freshness rule, not a safety
rule: any commit landing on `main` — including the automatic version bump after
every merge — invalidated an in-flight candidate and forced a rebuild plus a
fresh staging cycle.

Safety now comes from three independent bindings instead of tip equality: the
authorized revision must BE the staged candidate (`RELEASE_REVISION_NOT_STAGED`),
its tree must match (`MAIN_TREE_MISMATCH`), and it must remain reachable from
the authorizing `main` (`SOURCE_REVISION_NOT_IN_MAIN`). Version and `latest`
aliases are read from the staged commit rather than the authorization checkout,
and production-deploy no longer treats the authorization run's `head_sha` as the
application release revision.

Production additionally refuses any deploy that is not a descendant of the
running revision — proven from the running container's own
`org.opencontainers.image.revision` label, before backup or migrations. A
deliberate rollback requires a typed authorization bound to the exact
transition, not a boolean escape.

**Amended 2026-08-29 (fail-closed running-state observation):** an empty
revision observation is not proof that production has never been deployed.
The adapter first proves Docker daemon access and inventories the exact
production container. An existing container must expose a valid full-SHA
`org.opencontainers.image.revision` label; an unreadable daemon, failed
inventory/inspection, absent label, or malformed label refuses deployment
before anything touches the host: the gate is the first step after argument
validation, so it precedes the hotfix migration-evidence collection (which
pulls images and creates throwaway containers) as well as `deploy.sh`, the
backup, and migrations.

A genuine first deployment is a separate state: the Docker daemon is readable
and the exact production container is confirmed absent. Even then deployment
requires a typed bootstrap authorization naming `dotmac-sub-prod`, the exact
staged target revision, a change reference, and a reason. The document is
accepted only while the container is absent and cannot be combined with a
rollback authorization, hotfix mode, or post-migration resume. Bootstrap is
therefore explicit transition authority, not a reusable "skip anti-rollback"
flag.

**Amended 2026-08-28:** the release source branch changed from `dev` to `main`
when the dev-first hop was retired. Nothing else in this decision changed: the
image is still built once from an explicitly selected green SHA, staging still
accepts that exact digest, and production eligibility is still derived from the
recorded staging acceptance. The branch was the transport, not the guarantee.

**Owner:** Dotmac Sub release control plane

## Decision

The standard release path will build one immutable application image from the
final, green `main` release candidate. Staging will accept that exact OCI
digest. Production eligibility will be derived from green `main` CI, Git-tree
equality, ancestry, and the recorded staging acceptance. Registry-side
tagging may add version or `latest` aliases, but it must not create a second
application image.

The active candidate path uses the contracts through
`scripts/release_candidate_evidence.py`. The exact intended green `main` commit
is selected manually by full SHA, built once, and recorded as a digest-bound
artifact. The same Docker build derives a canonical product manifest from the
image's exact `SUB_ASSEMBLY` and `VERSION` and stores it inside the image, so the
OCI digest transitively binds those bytes. Candidate evidence records the
manifest digest and retains the canonical document as a separate evidence
artifact for downstream catalogue attestation. An open rolling version-bump
pull request is not deployment authority for an already-selected candidate;
version metadata and semver aliases are separate from digest eligibility.
Staging consumes and records that digest. The production authorization is
recorded separately after the staged tree reaches green `main`. The application
publisher no longer rebuilds on `main`; only the independent pinned
GenieACS runtime remains in `ghcr.yml`.

The release freeze is merge control, not deployment authority. While a
candidate, staging deployment, production authorization, or production
deployment workflow is queued or running, pull requests into `main` must not
merge. Branch pushes, pull request creation, and pull request updates remain
open. The freeze prevents `main` from moving underneath an in-flight candidate;
the selected SHA and OCI digest remain the facts that deployment consumes.

## Ownership and authoritative evidence

The release control plane owns candidate selection, artifact promotion,
production-eligibility, anti-rollback, and first-deployment bootstrap
decisions. Its authoritative inputs are:

- GitHub-hosted CI workflow conclusions for exact source and release commits;
- Git commit and tree identities supplied by GitHub;
- the immutable OCI manifest digest published to GHCR;
- the kernel-canonical product manifest embedded in that image and its
  `sha256:` document digest;
- the GitHub staging deployment result for that exact digest;
- the Docker daemon and exact-container observation from the named production
  host, including the running image's full revision label;
- explicit typed, exact-host/exact-revision bootstrap evidence when the
  production container is confirmed absent; and
- explicit, typed migration evidence for a production hotfix backup exception.

GHCR owns artifact bytes and aliases; it does not decide whether an artifact is
safe for production. GitHub-hosted CI owns test execution and its conclusions;
deployment hosts do not reinterpret or reproduce test results. Staging and
production scripts remain transport/operation adapters around the release
decision.

The release record has one canonical identity tuple:

```text
(source commit, source tree, OCI digest, product-manifest digest, build workflow run)
```

Staging acceptance must repeat the same source commit, tree, and digest.
Production authorization records the current `main` release commit. In the
main-only path this is normally the same commit as the staged source; its tree
must equal the source tree and its ancestry must contain the source commit.

## State model

The control plane derives these states from evidence rather than mutable tags:

1. **Built:** exact `main` source CI is green, one OCI digest exists, and its
   embedded product manifest verifies against the image's assembly and version.
2. **Staging accepted:** the staging deployment succeeded for the same source
   commit, source tree, and OCI digest.
3. **Main authorized:** exact `main` CI is green, its tree equals the staged
   source tree, and the staged source commit is in its ancestry.
4. **Production eligible:** all prior evidence remains exact and available.

A missing, pending, failed, cancelled, stale, or mismatched observation fails
closed. A changed source tree requires a new build and a new staging acceptance.
Attaching a new tag to a digest never changes its state.

## Backup policy

Backup behavior is owned by the identified deployment environment:

- Staging always skips the pre-deployment database backup through its exact-host
  adapter. A hotfix exception is not a staging control.
- Production requires a backup by default.
- Production may omit the backup only for an explicitly attributed hotfix whose
  complete migration-graph digest is unchanged, whose running and candidate
  image heads are identical, and whose database is already at those exact
  candidate heads.
- Missing or ambiguous hotfix evidence resolves to taking the backup.

Unrestricted production `SKIP_BACKUP=1` use is retired. The explicit production
adapter carries a non-secret change reference and reason, fingerprints both
images' complete migration trees, derives their heads, reads the database
heads, and re-evaluates the typed policy before `deploy.sh` permits a skip.

## Test and host boundary

Formatters, linters, type checks, security scans, unit tests, architecture
tests, integration tests, migration rehearsals, and browser/mobile tests run on
GitHub-hosted CI. They do not run on staging or production hosts.

Deployment hosts perform only bounded operational checks: host identity,
release evidence and digest verification, backup policy, current database
migration-state observation, migrations, schema/readiness checks, service
replacement, health checks, and rollback. These checks do not constitute a
replacement test suite.

The staging deployment worktree checks out the verified candidate at detached
`HEAD` and never advances, merges, resets, or force-updates a local branch.
Local branch refs are preserved operational context, not release evidence and
not an input to the release-control decision.

## Authority migration and cutover

### Old owner and paths

- The retired `.github/workflows/ghcr.yml` application job built on every `dev`
  and `main` push.
- The retired staging path treated each successful dev image build as a
  staging candidate.
- The retired production gate accepted a generic `SKIP_BACKUP=1` override and
  did not bind authorization to complete typed staging evidence.

### New owner and paths

- The explicit release-candidate workflow selects the exact current `main`
  tip intended for release and builds the application image once.
- A staging deployment record will bind acceptance to the immutable digest.
- The promotion workflow proves main tree equality, ancestry, main CI, and
  staging acceptance before adding production aliases to the same digest.
- The production adapter verifies the recorded source and release revisions
  and deploys by digest. The main-only path permits them to be identical.
- A typed backup-policy decision owns the staging skip, production default,
  and proven no-migration hotfix exception.

### Shadow and verification phase

The candidate and staging slices ran in shadow before cutover and completed an
accepted exact-digest rehearsal. The one promotion that first placed the new
workflow on GitHub's default branch used the previously active staging path
because `workflow_dispatch` cannot activate a workflow absent from the default
branch. The shadow publisher is now retired.

### Cutover gate

Cutover requires all of the following:

- one candidate build reports its immutable OCI digest;
- staging deploys and records success for that digest;
- main CI passes for a commit with the identical tree and required ancestry;
- the promotion workflow attaches aliases without invoking a Docker build;
- the production verifier approves the recorded source/release evidence;
- backup-policy tests prove staging skip, production default, and hotfix
  fail-closed behavior; and
- architecture tests prevent ordinary `main` pushes from restoring duplicate
  application builds or host-side test execution.

### Fallback retirement

Ordinary application publishing from `main` pushes, intermediate staging
triggers, and the generic production backup skip are retired. The emergency
path remains explicit, attributable, digest-pinned, and subject to the same
staging and production evidence rules.

## Planned implementation slices

1. Typed contracts and design evidence (complete).
2. Explicit one-time candidate build and digest-bound staging evidence
   (complete).
3. Main tree/ancestry authorization and registry-side digest promotion
   (complete).
4. Digest-based production verification and hotfix backup enforcement
   (complete).
5. Trigger cutover, duplicate publisher removal, and GenieACS build isolation
   (complete).
