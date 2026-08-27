from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
SHARD_SCRIPT = ROOT / "scripts/ci/select_test_shard.py"
INTEGRATION_SHARD_SCRIPT = ROOT / "scripts/ci/select_integration_shard.py"
POSTGRESQL_CLASSIFIER_SCRIPT = ROOT / "scripts/ci/classify_postgresql_changes.py"
SERVICE_READINESS_WORKFLOWS = {
    ROOT / ".github/workflows/ci.yml": ("ci-db", "ci-redis"),
    ROOT / ".github/workflows/e2e.yml": ("e2e-db", "e2e-redis"),
    ROOT / ".github/workflows/e2e-gate.yml": ("e2e-db", "e2e-redis"),
}


def _service_readiness_block(source: str, *, database: str, redis: str) -> str:
    start_marker = (
        "          for i in $(seq 1 30); do\n"
        f"            docker exec {database} pg_isready -U postgres"
    )
    redis_failure_marker = (
        f"          if ! docker exec {redis} redis-cli ping >/dev/null 2>&1; then\n"
    )
    start = source.index(start_marker)
    redis_failure = source.index(redis_failure_marker, start)
    end = source.index("          fi\n", redis_failure) + len("          fi\n")
    return (
        source[start:end]
        .replace(database, "DATABASE_SERVICE")
        .replace(redis, "REDIS_SERVICE")
    )


def _assert_service_readiness_blocks_match(blocks: tuple[str, ...]) -> None:
    assert blocks
    assert all(block == blocks[0] for block in blocks[1:])


def _assert_service_readiness_fails_closed(block: str) -> None:
    database_failure = block[
        block.index(
            "          if ! docker exec DATABASE_SERVICE pg_isready -U postgres"
        ) : block.index("          fi\n")
    ]
    redis_failure_start = block.index(
        "          if ! docker exec REDIS_SERVICE redis-cli ping"
    )
    redis_failure = block[
        redis_failure_start : block.index("          fi\n", redis_failure_start)
    ]

    for service, failure in (
        ("DATABASE_SERVICE", database_failure),
        ("REDIS_SERVICE", redis_failure),
    ):
        assert f"::error::{service} did not become ready within 60s" in failure
        assert f"docker logs {service} || true" in failure
        assert "            exit 1\n" in failure


def _load_shard_module():
    spec = importlib.util.spec_from_file_location("select_test_shard", SHARD_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_unit_shards_partition_all_unit_test_files_once(tmp_path: Path) -> None:
    module = _load_shard_module()
    expected = set(module._test_files())
    durations_path = tmp_path / "missing-durations.json"
    groups = [
        set(
            module.select_shard(
                shard=shard,
                shards=4,
                durations_path=durations_path,
            )
        )
        for shard in range(1, 5)
    ]

    assert set().union(*groups) == expected
    assert sum(len(group) for group in groups) == len(expected)


def test_integration_shards_partition_every_file_once() -> None:
    module = _load_module("select_integration_shard", INTEGRATION_SHARD_SCRIPT)
    # Recursive: the selector used a flat glob, so the first subdirectory added
    # under tests/integration/ would have been dropped from every shard with no
    # error and no skip. Kept in sync with the selector's own discovery.
    expected = set((ROOT / "tests/integration").rglob("test_*.py"))
    groups = [
        set(module.select_integration_shard(shard=shard, shards=4))
        for shard in range(1, 5)
    ]

    assert set().union(*groups) == expected
    assert sum(len(group) for group in groups) == len(expected)


def test_postgresql_classifier_is_narrow_and_fails_closed() -> None:
    """Exemptions stay narrow, and everything else triggers the lane.

    CONTRACT CHANGE: a root-level `tests/*.py` module used to be exempt. It is
    not, because the integration suite imports helpers from exactly there --
    `tests.staff_identity_fixtures`, `tests.referral_program_testkit`,
    `tests.prepaid_funding_helpers`, `tests.test_crm_ticket_pull` and
    `tests.test_integration_whatsapp_capability` today, with nothing stopping
    the next one. Editing such a module changed what the PostgreSQL lane
    executes while telling CI it could skip that lane.

    The surviving exemptions are proven rather than assumed: see
    `test_postgresql_lane_isolation.py`, which walks the lane's transitive
    import closure and fails if it ever reaches an exempt test package or any
    request/render entry point.
    """

    module = _load_module("classify_postgresql_changes", POSTGRESQL_CLASSIFIER_SCRIPT)

    assert not module.classify_postgresql_changes(
        ("templates/admin/inbox/index.html", "static/js/inbox.js")
    ).required
    assert not module.classify_postgresql_changes(
        ("tests/architecture/test_ci_pipeline.py",)
    ).required
    assert module.classify_postgresql_changes(("tests/test_inbox_ui.py",)).required
    assert module.classify_postgresql_changes(("tests/conftest.py",)).required
    assert module.classify_postgresql_changes(
        ("tests/integration/test_inbox.py",)
    ).required
    assert module.classify_postgresql_changes(
        ("app/services/team_inbox_read.py",)
    ).required
    assert module.classify_postgresql_changes(("scripts/ci/anything.py",)).required
    assert module.classify_postgresql_changes(()).required


def test_unit_shards_prefer_measured_durations_over_source_size(
    tmp_path: Path,
) -> None:
    module = _load_shard_module()
    paths = module._test_files()
    durations = {path.relative_to(ROOT).as_posix(): 1.0 for path in paths}
    slowest = min(paths, key=lambda path: path.stat().st_size)
    durations[slowest.relative_to(ROOT).as_posix()] = 1000.0
    durations_path = tmp_path / "test-durations.json"
    durations_path.write_text(
        json.dumps({"schema_version": 1, "durations": durations}),
        encoding="utf-8",
    )

    groups = [
        module.select_shard(
            shard=shard,
            shards=4,
            durations_path=durations_path,
        )
        for shard in range(1, 5)
    ]

    assert groups[0] == [slowest]


def test_ci_uses_one_named_application_cache_after_publisher_cutover() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    e2e_workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")
    ghcr_workflow = (ROOT / ".github/workflows/ghcr.yml").read_text(encoding="utf-8")

    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "scope=dotmac-sub-application" in workflow
    assert "github.event_name != 'push'" in workflow
    assert "docker push" not in workflow
    assert "docker/build-push-action" not in e2e_workflow
    assert 'image_tag="sha-${GITHUB_SHA::7}"' in e2e_workflow
    # ghcr.yml is now isolated to the pinned GenieACS runtime. Application
    # bytes are built once by the explicit candidate workflow and later
    # receive production aliases without another build.
    assert ghcr_workflow.count("uses: docker/build-push-action@v6") == 1
    assert "branches: [main]" in ghcr_workflow
    assert "context: .\n" not in ghcr_workflow
    assert "scope=genieacs" in ghcr_workflow
    assert "-genieacs:${{ steps.version.outputs.version }}" in ghcr_workflow
    assert "genieacs:latest" not in ghcr_workflow

    candidate_workflow = (ROOT / ".github/workflows/release-candidate.yml").read_text(
        encoding="utf-8"
    )
    assert candidate_workflow.count("uses: docker/build-push-action@v6") == 1
    assert "on:\n  workflow_dispatch:" in candidate_workflow
    assert "on:\n  push:" not in candidate_workflow
    assert "scope=dotmac-sub-application" in candidate_workflow
    assert "type=raw,value=latest" not in candidate_workflow

    promotion_workflow = (ROOT / ".github/workflows/release-promotion.yml").read_text(
        encoding="utf-8"
    )
    assert "docker buildx imagetools create" in promotion_workflow
    assert "docker/build-push-action" not in promotion_workflow


def test_copied_service_readiness_blocks_stay_in_parity_and_fail_closed() -> None:
    blocks = tuple(
        _service_readiness_block(
            workflow.read_text(encoding="utf-8"),
            database=services[0],
            redis=services[1],
        )
        for workflow, services in SERVICE_READINESS_WORKFLOWS.items()
    )

    _assert_service_readiness_blocks_match(blocks)
    for block in blocks:
        _assert_service_readiness_fails_closed(block)


def test_service_readiness_parity_guard_is_sensitive_to_one_copy_drifting() -> None:
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    block = _service_readiness_block(
        source,
        database="ci-db",
        redis="ci-redis",
    )
    drifted = block.replace("docker logs REDIS_SERVICE || true", "true", 1)

    with pytest.raises(AssertionError):
        _assert_service_readiness_blocks_match((block, block, drifted))


def test_service_readiness_guard_is_sensitive_to_fail_open_regression() -> None:
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    block = _service_readiness_block(
        source,
        database="ci-db",
        redis="ci-redis",
    )
    fail_open = block.replace("            exit 1\n", "", 1)

    with pytest.raises(AssertionError):
        _assert_service_readiness_fails_closed(fail_open)


def test_ci_removes_workstation_venv_pointer_before_cache_and_restore() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    setup_action = (ROOT / ".github/actions/setup-ci-python/action.yml").read_text(
        encoding="utf-8"
    )

    assert "if [ -L .venv ]; then" in workflow
    assert "if [ -L .venv ]; then" in setup_action
    assert workflow.index("if [ -L .venv ]; then") < workflow.index("path: .venv")
    assert setup_action.index("if [ -L .venv ]; then") < setup_action.index(
        'tar -xzf "$RUNNER_TEMP/python-environment/python-venv.tar.gz"'
    )


def test_ci_pins_and_shares_one_poetry_installation() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    setup_action = (ROOT / ".github/actions/setup-ci-python/action.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    poetry_version = project["tool"]["poetry"].get("requires-poetry")
    lock_header = (ROOT / "poetry.lock").read_text(encoding="utf-8").splitlines()[0]

    assert poetry_version == "2.4.1"
    assert f'POETRY_VERSION: "{poetry_version}"' in workflow
    assert "version: ${{ env.POETRY_VERSION }}" in workflow
    assert f"ENV POETRY_VERSION={poetry_version} " in dockerfile
    assert f"generated by Poetry {poetry_version} " in lock_header
    assert "poetry-home.tar.gz" in workflow
    assert "poetry-home.tar.gz" in setup_action
    assert "snok/install-poetry" not in setup_action


def test_ci_persists_unit_test_duration_history() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "unit-test-durations-v1-" in workflow
    assert "scripts/ci/merge_test_durations.py" in workflow
    assert "make test-ci-shard" in workflow
    assert "test-ci-shard:" in makefile
    assert "--ci-durations-output" in makefile
    assert "--durations=50" in makefile


def test_ci_test_jobs_fail_closed_instead_of_hanging_indefinitely() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    unit_job = workflow[
        workflow.index("  unit-shards:\n") : workflow.index("  architecture:\n")
    ]
    architecture_job = workflow[
        workflow.index("  architecture:\n") : workflow.index("  test:\n")
    ]

    assert "    timeout-minutes: 30\n" in unit_job
    assert "    timeout-minutes: 30\n" in architecture_job
    assert "CI_UNIT_TEST_WORKERS ?= 4" in makefile
    assert "CI_TEST_TIMEOUT_SECONDS ?= 180" in makefile
    assert "-n $(CI_UNIT_TEST_WORKERS)" in makefile
    assert "--max-worker-restart=0" in makefile
    assert "--timeout=$(CI_TEST_TIMEOUT_SECONDS)" in makefile
    assert "--timeout-method=signal" in makefile
    assert "pytest-timeout==2.4.0" in project["dependency-groups"]["dev"]


def test_docs_only_changes_report_each_required_unit_shard() -> None:
    """Expand the matrix before taking the documentation-only no-op path."""

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    unit_job = workflow[
        workflow.index("  unit-shards:\n") : workflow.index("  migration-sequence:\n")
    ]

    assert "matrix:\n        shard: [1, 2, 3, 4]" in unit_job
    assert "if: always() && needs.changes.result == 'success'" in unit_job
    assert "needs.changes.outputs.docs-only == 'true'" in unit_job
    assert "needs.python-environment.result == 'success'" in unit_job
    assert "- name: Documentation-only change" in unit_job
    assert 'run: echo "Unit-test shard ${{ matrix.shard }}' in unit_job
    assert unit_job.count("if: needs.changes.outputs.docs-only != 'true'") == 6


def test_tests_is_the_stable_aggregate_for_unit_and_architecture_results() -> None:
    """Matrix children remain internal evidence behind one stable check name."""

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    aggregate = workflow[workflow.index("  test:\n") : workflow.index("  coverage:\n")]

    assert "    name: Tests\n" in aggregate
    assert (
        "needs: [changes, python-environment, unit-shards, architecture]" in aggregate
    )
    assert "UNIT_RESULT: ${{ needs.unit-shards.result }}" in aggregate
    assert "ARCHITECTURE_RESULT: ${{ needs.architecture.result }}" in aggregate
    assert 'test "$UNIT_RESULT" = "success"' in aggregate
    assert 'test "$ARCHITECTURE_RESULT" = "success"' in aggregate


def test_ci_change_classifier_fetches_missing_comparison_base() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    classifier = workflow[
        workflow.index("  changes:\n") : workflow.index("  python-environment:\n")
    ]

    existence_check = 'git cat-file -e "$base^{commit}"'
    targeted_fetch = 'git fetch --no-tags --depth=1 origin "$base"'
    path_diff = 'git diff --name-only "$base" HEAD'

    assert existence_check in classifier
    assert targeted_fetch in classifier
    assert classifier.index(existence_check) < classifier.index(targeted_fetch)
    assert classifier.index(targeted_fetch) < classifier.index(path_diff)


def test_ci_change_classifier_does_not_resolve_a_base_from_shallow_roots() -> None:
    """`git rev-list --max-parents=0 HEAD` returns two shas for a merge commit.

    Under `fetch-depth: 2` the shallow clone grafts both parents of a merge
    commit, so both look parentless. The two shas were then interpolated into a
    single refspec and the job died with `fatal: invalid refspec`, which
    happened for real when a branch was created at a promotion merge commit.

    With no base there is nothing to diff, so the classifier must fail safe to
    application rather than invent one.
    """

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    classifier = workflow[
        workflow.index("  changes:\n") : workflow.index("  python-environment:\n")
    ]
    # Assert against what actually runs. The comment explaining why this
    # fallback was removed necessarily names the command it removed.
    executed = "\n".join(
        line for line in classifier.splitlines() if not line.strip().startswith("#")
    )

    assert "--max-parents=0" not in executed

    zero_sha_guard = '[ "$base" = "0000000000000000000000000000000000000000" ]'
    assert zero_sha_guard in executed

    # The no-base branch must short-circuit to the complete matrix before any
    # git command consumes "$base"; running everything is the safe default.
    #
    # The evidence used to be the `application=true` output, which was removed
    # because no job ever consumed it. `postgresql-required=true` is the
    # surviving proof that this branch runs everything.
    tail = executed[executed.index(zero_sha_guard) :]
    assert tail.index("postgresql-required=true") < tail.index('git cat-file -e "$base')


def test_production_dependency_group_excludes_ci_tools() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
    dev_tools = {
        "bandit",
        "import-linter",
        "mypy",
        "playwright",
        "pre-commit",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "pytest-timeout",
        "pytest-xdist",
        "ruff",
        "vulture",
    }

    assert "dev" not in project["project"].get("optional-dependencies", {})
    locked_groups = {
        package["name"]: set(package["groups"])
        for package in lock["package"]
        if package["name"] in dev_tools
    }
    assert locked_groups
    assert all("main" not in groups for groups in locked_groups.values())


def test_ci_retains_pre_merge_and_promotion_postgresql_gate() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    # Both events must still cover main and dev. Batch branches run these same
    # gates instead of meeting them for the first time at the batch -> dev merge.
    protected_branches = "branches: [main, dev, 'integration/**', 'consolidate/**']"
    assert workflow.count(protected_branches) == 2
    assert "make test-integration" in workflow
    assert "poetry run alembic upgrade head" not in workflow
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "python -m scripts.ci.migrated_test_database" in makefile


def test_fresh_test_databases_bootstrap_dispatcher_roles_before_alembic_only() -> None:
    """Fresh test clusters need roles; ordinary and production migrations do not."""

    bootstrap = "scripts/bootstrap_outbox_dispatcher_roles.py"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    e2e_workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")
    e2e_gate = (ROOT / ".github/workflows/e2e-gate.yml").read_text(encoding="utf-8")

    helper = makefile[
        makefile.index("bootstrap-test-database-roles:") : makefile.index(
            "test-integration:"
        )
    ]
    assert 'BOOTSTRAP_DATABASE_URL="$${TEST_DATABASE_URL}"' in helper
    assert bootstrap in helper
    assert helper.index("parse_test_database_target") < helper.index(bootstrap)
    assert "postgresql://" not in helper

    integration = makefile[
        makefile.index("test-integration:") : makefile.index("INTEGRATION_SHARD ?=")
    ]
    shard = makefile[
        makefile.index("test-integration-shard:") : makefile.index("test-architecture:")
    ]
    for recipe in (integration, shard):
        assert recipe.index("bootstrap-test-database-roles") < recipe.index(
            "scripts.ci.migrated_test_database"
        )

    ci_migration = ci_workflow[
        ci_workflow.index(
            "- name: Run migrations and application health check"
        ) : ci_workflow.index("- name: Cleanup")
    ]
    nightly_migration = e2e_workflow[
        e2e_workflow.index("- name: Migrate + seed") : e2e_workflow.index(
            "- name: Start application"
        )
    ]
    gate_migration = e2e_gate[
        e2e_gate.index(
            "- name: Migrate + seed + start app from this PR's code"
        ) : e2e_gate.index("- name: Run Playwright suite")
    ]
    for workflow_step in (ci_migration, nightly_migration, gate_migration):
        assert 'BOOTSTRAP_DATABASE_URL="$DATABASE_URL"' in workflow_step
        assert workflow_step.index(bootstrap) < workflow_step.index(
            "alembic upgrade heads"
        )
        assert "BOOTSTRAP_DATABASE_URL=postgresql" not in workflow_step
        assert 'echo "$BOOTSTRAP_DATABASE_URL"' not in workflow_step

    assert "POSTGRES_DB: dotmac_sub_test" in ci_workflow
    assert "POSTGRES_DB=dotmac_sub_ci" in ci_workflow
    assert "POSTGRES_DB=dotmac_sub_e2e" in e2e_workflow
    assert "POSTGRES_DB=dotmac_sub_e2e" in e2e_gate

    production_sources = (
        (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8"),
        (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8"),
    )
    assert all(bootstrap not in source for source in production_sources)
    for start, end in (
        ("migrate:", "new-migration:"),
        ("docker-migrate:", "# ─── Host-build fallback guard"),
        ("prod-migrate:", "# ─── GHCR deploy"),
    ):
        production_recipe = makefile[makefile.index(start) : makefile.index(end)]
        assert bootstrap not in production_recipe
