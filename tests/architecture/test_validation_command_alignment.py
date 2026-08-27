"""Keep local, agent, and CI validation commands on one executable owner."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_makefile_owns_parallel_non_integration_suite() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "UNIT_TEST_PATHS := tests/ --ignore=tests/integration --ignore=tests/e2e"
        in makefile
    )
    assert "UNIT_TEST_WORKERS ?= auto" in makefile
    assert "-n $(UNIT_TEST_WORKERS)" in makefile
    assert "test-ci:" in makefile
    assert "test-integration:" in makefile


def test_ci_and_agent_guidance_call_makefile_validation_owners() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "make test-ci-shard" in workflow
    assert "scripts/ci/select_test_shard.py" in makefile
    assert "make test-architecture" in workflow
    assert "run: make test-integration-shard" in workflow
    assert "python -m scripts.ci.migrated_test_database" in makefile
    assert "run: poetry run alembic upgrade head" not in workflow
    assert "make test-architecture" in guidance
    assert "make test\n" in guidance
    assert "make test-integration" in guidance


def test_e2e_workflow_runs_the_browser_it_installs() -> None:
    workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")

    assert "playwright install chromium --with-deps" in workflow
    assert 'PLAYWRIGHT_BROWSER: "chromium"' in workflow
    assert 'E2E_JWT_SECRET="$(openssl rand -hex 32)"' in workflow
    assert '-e JWT_SECRET="$E2E_JWT_SECRET"' in workflow
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in workflow
    assert "POSTGRES_PASSWORD" not in workflow
    assert (
        'TEST_DATABASE_URL: "postgresql+psycopg://postgres@127.0.0.1:55432/'
        'dotmac_sub_e2e"'
    ) in workflow
    assert "type: choice" in workflow
    assert "- service-teams" in workflow
    assert 'if [ "$E2E_SUITE" = "service-teams" ]; then' in workflow
    assert "tests/playwright/e2e/test_service_teams.py" in workflow


def _logical_makefile_lines(makefile: str) -> list[str]:
    """Join backslash continuations, so one recipe is one string.

    A Makefile recipe routinely spans several physical lines. Scanning them
    individually finds `poetry run pytest` on one and `-p scripts.ci....` on the
    next, and concludes -- wrongly -- that no recipe does both.
    """

    joined: list[str] = []
    buffer = ""
    for line in makefile.splitlines():
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append(buffer + line)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _plugin_loading_recipes(makefile: str) -> list[str]:
    return [
        line
        for line in _logical_makefile_lines(makefile)
        if "poetry run pytest" in line and "-p scripts.ci." in line
    ]


def test_every_target_loading_a_repository_plugin_sets_pythonpath() -> None:
    """`-p <module>` resolves at pytest STARTUP, before rootdir reaches sys.path.

    `poetry run pytest` runs a console script, so the working directory is not
    on `sys.path` the way it is under `python -m pytest`. A target that loads a
    repository-local plugin without exporting PYTHONPATH therefore dies with
    `No module named 'scripts'` -- but only under the console script, which is
    precisely why a local `python -m pytest` check does not reproduce it.

    All four PostgreSQL shards failed this way on the first duration-balanced
    run.
    """

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipes = _plugin_loading_recipes(makefile)
    assert recipes, "no target loads a repository-local pytest plugin any more"
    for recipe in recipes:
        assert "PYTHONPATH=" in recipe, (
            "a target loads a repository-local pytest plugin without putting the "
            f"repository on sys.path: {recipe.strip()!r}"
        )


def test_the_pythonpath_guard_bites() -> None:
    """Sensitivity proof: the guard must fail on the shape that broke CI.

    Both halves matter. The detector has to FIND the recipe once continuations
    are joined -- a scan that found nothing would pass the check above for the
    wrong reason -- and it has to reject the recipe once PYTHONPATH is removed.
    """

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipes = _plugin_loading_recipes(makefile)
    assert len(recipes) >= 2, (
        "expected the unit and integration shard targets to be found; "
        f"found {len(recipes)}"
    )

    broken = makefile.replace('PYTHONPATH="$(CURDIR)" ', "")
    broken_recipes = _plugin_loading_recipes(broken)
    assert broken_recipes, "the detector stopped finding recipes after the mutation"
    assert all("PYTHONPATH=" not in recipe for recipe in broken_recipes), (
        "removing PYTHONPATH did not change what the guard sees, so the guard "
        "would pass against the exact shape that failed every PostgreSQL shard"
    )
