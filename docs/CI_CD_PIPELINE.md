# CI/CD pipeline

The `CI` workflow is the deployment-quality owner for pull requests targeting
`dev` or `main`, merge groups, and promotion through `dev`, `develop`, and
`main`.

## Required gates

Application changes retain all release gates:

- lint, formatting, typing, import-boundary, pre-commit, and blocking security
  checks;
- the complete non-integration suite, split deterministically across four
  runners, plus the explicit four-worker architecture suite;
- Alembic and integration tests against PostgreSQL before merge and again on
  promotion branches; and
- one production Docker build followed by migration and health checks.

Pull requests receive the Docker migration and health gate. Branch pushes do
not repeat that build in CI: `ghcr.yml` is the sole publisher for `dev` and
`main` and remains the named trigger for staging deployment. Dev receives only
its immutable SHA tag; only the default branch receives `latest`. The BuildKit
cache scope `dotmac-sub-application` is shared by pre-merge validation and
publication so unchanged layers are reused without unrelated builds
overwriting the cache history.

Browser E2E remains nightly and manually dispatchable in `e2e.yml`. It pulls
the immutable `sha-<commit>` image published by CI, rather than rebuilding it,
and is not a per-change merge gate.

## Dependency reuse

`python-environment` is the only CI job that resolves and installs Poetry
dependencies. Its exact environment cache is keyed by operating system, Python
version, `poetry.lock`, and `pyproject.toml`. The environment is then packed as
a one-day workflow artifact and restored by the local
`setup-ci-python` composite action. Parallel jobs therefore do not each install
the full dependency set on a cold workflow. Both setup paths first remove the
repository's workstation-only `.venv` symlink from the runner checkout so the
CI environment is always created inside the writable workspace.

## Change classification

A change is documentation-only only when every changed path is under `docs/`
or is a Markdown file. Required check names still report, but application
tests, PostgreSQL services, Docker builds, and security scans do no heavy work.
Workflow, dependency, migration, test, and application changes always receive
the full pipeline. Manual runs also always execute the full pipeline.

## Unit-test sharding

`scripts/ci/select_test_shard.py` assigns every non-integration,
non-architecture test file to exactly one of four shards. Assignment is stable
and greedily balanced by source size; each shard still uses pytest-xdist.
Coverage data from all shards is combined before the XML report is published.

Architecture guards remain a separate required group. This prevents the same
architecture suite from being collected and run as part of every general unit
shard while retaining the repository's measured four-worker architecture
default.
