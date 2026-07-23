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

    assert "run: make test-ci" in workflow
    assert "run: make test-integration" in workflow
    assert "make test-architecture" in guidance
    assert "make test\n" in guidance
    assert "make test-integration" in guidance
