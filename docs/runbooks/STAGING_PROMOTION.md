# Dev-to-staging-to-main promotion

This repository promotes release changes through `dev` and the staging
environment before `main`. A feature branch must not bypass this path.

## Promotion sequence

1. Open the feature pull request against `dev` with the appropriate
   `version:major`, `version:minor`, `version:patch`, or `version:none` label.
   Do not edit `VERSION` in the source pull request; the version-bump workflow
   owns the separate rolling bump pull request after merge.
2. Require CI, Mobile CI, and Version Impact to pass before merging.
3. Merge into `dev`. Push CI and the GHCR workflow then validate and build the
   exact `origin/dev` commit.
4. Deploy the immutable `sha-<short-commit>` image only to the staging host.
5. Complete staging acceptance against `http://10.120.121.20:8001`.
6. Merge the rolling version-bump PR for `dev`, when one exists, and repeat the
   staging gate for that final versioned dev commit.
7. Create `release/promote-<version>-<short-sha>` at the exact accepted `dev`
   commit without adding another commit. Promote that branch to `main` without
   unrelated changes. The promotion PR uses `version:none` and contains the
   line `Staged dev SHA: <full 40-character SHA>`.
8. Require main CI and the main GHCR build to pass. Only the default branch may
   receive the moving `latest` image tag.
9. Deploy the immutable main image to production only after Michael explicitly
   requests the named production host.

The main build remains separate from the dev build. GitHub Actions layer
caching makes the second build faster while preserving commit-specific OCI
revision evidence.

## Main admission policy

`.github/workflows/promotion-policy.yml` is the fail-closed admission owner for
pull requests targeting `main`. Configure its `Promotion Policy` job as a
required status check on `main`.

It accepts only these cases:

1. A `release/promote-*` branch whose head is contained in `dev`, whose exact
   SHA has a latest successful `staging` deployment, whose body declares that
   SHA, and whose sole version-impact label is `version:none`.
2. `automation/version-bump-main`, labeled `version:none`, changing exactly the
   eight files owned by `scripts/bump_version.py`.
3. A `hotfix/*` branch carrying `hotfix` and `version:patch`, approved by
   someone other than its author, with non-empty `Incident:`, `Why dev was
   bypassed:`, and `Back-sync plan:` lines.

The hotfix path is an incident exception, not an alternative delivery lane.
The label alone does not authorize a bypass.

## Main-to-dev reconciliation

`.github/workflows/main-dev-sync.yml` runs after every `main` update. When
`dev` does not contain `main`, it:

1. branches from the current `origin/dev`;
2. creates a real `--no-ff` merge of `origin/main`;
3. updates `automation/sync-main-to-dev` with force-with-lease;
4. opens or updates a `version:none` pull request into `dev`.

The workflow never pushes directly to `dev`. Require normal CI on the
reconciliation pull request and merge it with a merge commit. Do not squash it:
the merge parent is the repair evidence that prevents recurring ancestry
divergence. Merge conflicts fail the workflow and require a reviewed manual
reconciliation.

The workflow authenticates as a dedicated GitHub App so its pull-request event
starts normal CI. Configure:

- repository variable `RELEASE_AUTOMATION_APP_ID`;
- repository secret `RELEASE_AUTOMATION_APP_PRIVATE_KEY`, sourced from an
  approved OpenBao-backed secret pointer;
- App permissions limited to repository contents, pull requests, issues, and
  metadata required to update the reconciliation branch and PR.

Never put the App private key or token in the repository, logs, workflow
inputs, or pull-request text.

## Protection activation

After this change reaches `main`:

1. Create the `hotfix` label.
2. Run `Promotion Policy` once, then add that exact job name to the required
   `main` status checks.
3. Require pull requests and at least one approval, dismiss stale approvals,
   require conversation resolution, and block direct pushes, force pushes, and
   administrator bypass.
4. Require merge commits for promotion and reconciliation PRs so ancestry is
   preserved.
5. Configure the release-automation GitHub App variable and secret above.
6. Manually dispatch `Reconcile main into dev` and merge its green PR to
   establish the first enforced synchronization boundary.

Do not call the controls active until the required status check and GitHub
ruleset are both verified from a deliberately rejected ordinary PR to `main`.

## Automatic staging deployment contract

`.github/workflows/staging-deploy.yml` is disabled unless the repository
variable `STAGING_AUTO_DEPLOY_ENABLED` is exactly `true`. Do not enable it until
all of the following are true:

- A runner registered specifically to `michaelayoade/dotmac_sub` is installed
  on the staging host and has the labels `self-hosted`, `linux`, `x64`, and
  `dotmac-sub-staging`.
- The runner service uses the staging operator account with access to Docker,
  the persistent checkout, the deploy lock, and the database-backup directory.
- The runner account is already authenticated to
  `ghcr.io/michaelayoade/dotmac_sub` with read-only package access. Never place
  that credential in the repository or runner labels.
- The GitHub `staging` environment defines
  `STAGING_DEPLOY_DIR=/home/dotmac/projects/dotmac_sub`.
- The persistent checkout contains the staging `.env` and the required
  host-local `docker-compose.override.yml`.
- The tracked persistent checkout is clean. The workflow refuses to discard
  tracked changes and updates local `dev` only by fast-forward.

The workflow accepts only a successful push-triggered GHCR build from this
repository's current `dev` tip. It waits for the CI and Mobile CI push workflows
for the exact same commit, rejects stale or failed candidates, verifies that
Celery Beat is absent, verifies the private `10.120.121.20:8001` binding, and
then invokes the hardened deployment owner with the staging-only proxy opt-out.

## Failure behavior

- A missing runner, disabled repository switch, wrong host path, dirty tracked
  checkout, stale dev SHA, failed check, missing image, unexpected port, or
  active Celery Beat prevents deployment.
- `scripts/deploy.sh` still owns backup, migration, candidate health, primary
  health, worker readiness, rollback, and image-retention behavior.
- A failed staging deployment never authorizes promotion to `main`.
- Missing promotion evidence, a mutable label without the required incident
  evidence, an unavailable release-automation credential, or a reconciliation
  conflict fails closed and requires operator review.
