# PR #3659 isolated release: baseline preflight

Status: preflight only; no application candidate selected or deployment authorized.

On 2026-10-19 the requesting operator explicitly approved a one-time release
exception: deploy only PR #3659 to staging, complete acceptance, then deploy the
same authorized digest to `dotmac-sub-prod` (`selfcare.dotmac.io`). The exception
does not authorize any other pending application changes, direct production
patching, bypassing branch/environment approval, or skipping CI/staging/digest
authorization. Architecture tests must run only on GitHub CI, never locally.

## Why a baseline observation is necessary

The last recorded production deployment is older than PR #3659, but a workflow
record is not proof of the container currently running. Local SSH cannot verify
the production host identity, and the operator has no SSH access. Do not disable
host-key checks, accept an unverified key, assume the old deployment record is
current, or repurpose a mutating infrastructure repair to obtain this evidence.

The reviewed `Observe PR 3659 release baseline` workflow therefore runs through
the existing trusted production runner. It requires the exact protected-main
source and explicitly named target, plus the existing protected production
environment approval. No SSH connection or credential export is involved.
Only Docker `container inspect` and `image inspect` are executed. The observer
checks the host's existing APP_ENV/SERVER_NAME contract without exporting its
environment. It records the running container/image IDs, image source revision,
source tree and unique locally resolving application-repository digest, then
rechecks container identity to detect concurrent changes. It performs no pull,
exec, build, migration, restart, checkout in the deployment directory, or test.
Its report is written outside the deployment directory and uploaded as an
observation artifact. Missing or ambiguous identity fails closed.

An Actions environment observation record is not evidence that a new application
was deployed. This workflow's only output is baseline.json, whose kind explicitly
identifies a read-only observation. It is not candidate.json, acceptance.json,
or authorization.json and must never be admitted as one of those documents.

## Remaining gates after observation

1. Confirm the observed source/digest still represents production and retain it
   as the rollback baseline. Read the exact merged PR #3659 diff.
2. Prepare an isolated candidate derived from that source, containing exactly the
   PR's application diff. Prove its tree against an independently applied patch;
   do not reset main, revert unrelated main changes, or cherry-pick later work.
3. Land separately reviewed, exact-source exception support in the release
   verifiers. The current main-tip and source-in-main rules still reject the
   isolated branch. This preflight DOES NOT alter or bypass those rules or claim
   they already support the requested release.
4. Run all prescribed CI on the exact admitted candidate, build it once, and
   retain the immutable image digest. No release may proceed on partial checks.
5. Deploy that digest to the named staging host and record application acceptance:
   unlinked spam resolution, linked Lead/Customer refusal, pending/ambiguous
   identity refusal, permissions, audit, replay and ordinary-resolution behavior.
6. Authorize only the same accepted digest, retain the normal protected production
   approval and backup, re-observe production's baseline, and deploy to
   dotmac-sub-prod. Refuse baseline drift or any broader source scope.

Protected-branch review of this preflight remains required. This file records
the request and scope; it is not a self-approved feature-freeze exception or a
standing alternative release branch. No entry is added to feature_freeze_exceptions.
The later exception support must be one-use/exact-source and retired after this
release or explicit abandonment. Existing normal release behavior remains intact.
