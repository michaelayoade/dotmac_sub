"""Prove the reachability premises the PostgreSQL change classifier relies on.

`scripts/ci/classify_postgresql_changes.py` lets a change skip the PostgreSQL
lane when every changed path is outside it.  Two of its exemptions are claims
about reachability rather than statements about the path itself:

- ``ISOLATED_TEST_PACKAGES`` -- test packages the lane never loads; and
- ``templates``/``static`` -- presentation assets the lane never reads.

ADR-0018 rule 23: an exemption states an ENFORCEABLE premise, or the region is
unmonitored rather than exempt.  This module is that enforcement, and it carries
its own sensitivity proof: every detector is driven against a synthetic source
that MUST trip it, so a detector that quietly stopped working cannot pass by
finding nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.classify_postgresql_changes import (
    ISOLATED_TEST_PACKAGES,
    NON_DATABASE_TREES,
)
from tests.architecture.postgresql_lane_lib import (
    ALL_RENDER_FAMILIES,
    REPOSITORY_ROOT,
    TESTS_ROOT,
    RenderFamily,
    build_closure,
    find_render_entry_points,
    find_unresolvable_imports,
)

INTEGRATION_ROOT = TESTS_ROOT / "integration"


def _lane_entry_points() -> list[Path]:
    """The files pytest itself collects when the PostgreSQL lane runs."""

    entry_points = sorted(INTEGRATION_ROOT.rglob("*.py"))
    assert entry_points, "the PostgreSQL lane has no entry points; the guard is blind"
    return entry_points


@pytest.fixture(scope="module")
def lane_closure() -> dict[Path, str]:
    return build_closure(_lane_entry_points())


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


# --------------------------------------------------------------------------
# The closure must actually be a closure
# --------------------------------------------------------------------------


def test_the_closure_follows_imports_out_of_the_integration_package(
    lane_closure: dict[Path, str],
) -> None:
    """A collapsed closure would pass every check below by reaching nothing."""

    reached = {_relative(path) for path in lane_closure}
    assert len(lane_closure) > len(_lane_entry_points())
    for helper in (
        "tests/staff_identity_fixtures.py",
        "tests/referral_program_testkit.py",
        "tests/prepaid_funding_helpers.py",
        # Reached only transitively, through prepaid_funding_helpers: proof the
        # walk does not stop at depth one.
        "tests/prepaid_funding_test_support.py",
    ):
        assert helper in reached, f"{helper} is loaded by the lane but was not reached"


def test_the_closure_includes_every_ancestor_conftest(
    lane_closure: dict[Path, str],
) -> None:
    """Nested conftest loading is import surface even with no explicit import."""

    reached = {_relative(path) for path in lane_closure}
    assert "tests/conftest.py" in reached
    assert "tests/integration/conftest.py" in reached


# --------------------------------------------------------------------------
# The premises themselves
# --------------------------------------------------------------------------


def test_no_isolated_test_package_is_reachable_from_the_postgresql_lane(
    lane_closure: dict[Path, str],
) -> None:
    offenders = sorted(
        _relative(path)
        for path in lane_closure
        if (parts := path.relative_to(REPOSITORY_ROOT).parts)
        and len(parts) > 1
        and parts[0] == "tests"
        and parts[1] in ISOLATED_TEST_PACKAGES
    )
    assert not offenders, (
        "The PostgreSQL lane now reaches test packages that "
        "classify_postgresql_changes.py exempts from triggering it: "
        f"{offenders}. Either break the import or remove the package from "
        "ISOLATED_TEST_PACKAGES."
    )


def test_every_isolated_test_package_exists() -> None:
    """An allowlist entry naming nothing is an exemption nobody can audit."""

    missing = sorted(
        package
        for package in ISOLATED_TEST_PACKAGES
        if not (TESTS_ROOT / package).is_dir()
    )
    assert not missing, (
        f"ISOLATED_TEST_PACKAGES names test packages that do not exist: {missing}"
    )


def test_the_postgresql_lane_has_no_request_or_render_entry_point(
    lane_closure: dict[Path, str],
) -> None:
    """`templates` and `static` are exempt only while this holds."""

    assert {"templates", "static"} <= set(NON_DATABASE_TREES), (
        "This premise exists to justify the templates/static exemptions; if "
        "they were removed from NON_DATABASE_TREES, retire the premise too."
    )
    findings = [
        str(finding)
        for path, source in lane_closure.items()
        for finding in find_render_entry_points(source, _relative(path))
    ]
    assert not findings, (
        "The PostgreSQL lane now reaches a request/render entry point, so a "
        "templates/ or static/ change can affect it while being exempt from "
        f"triggering it: {sorted(findings)}."
    )


def test_the_postgresql_lane_uses_no_unresolvable_import_form(
    lane_closure: dict[Path, str],
) -> None:
    """An import the walker cannot follow makes the closure incomplete.

    An incomplete closure cannot support an exemption, so an unknown dynamic
    import form fails the proof rather than being skipped.
    """

    findings = [
        str(finding)
        for path, source in lane_closure.items()
        for finding in find_unresolvable_imports(source, _relative(path))
    ]
    assert not findings, (
        "The PostgreSQL lane uses an import form this guard cannot follow, so "
        "its isolation can no longer be proven: "
        f"{sorted(findings)}."
    )


# --------------------------------------------------------------------------
# Sensitivity: every detector must bite on a source built to trip it
# --------------------------------------------------------------------------


RENDER_FIXTURES: dict[str, str] = {
    RenderFamily.test_client: """
from fastapi.testclient import TestClient
from app.main import app

def test_it():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
""",
    RenderFamily.httpx_asgi: """
import httpx

async def test_it(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("/health")
""",
    RenderFamily.client_wrapper: """
from tests.support import admin_client

def test_it():
    response = admin_client().get("/admin")
    assert response.status_code == 200
""",
    RenderFamily.template_render: """
from jinja2 import Environment

def test_it(env: Environment):
    assert env.get_template("admin/dashboard.html").render(user=None)
""",
    RenderFamily.asgi_app: """
from app.main import create_app

def test_it():
    assert create_app() is not None
""",
}


@pytest.mark.parametrize("family", sorted(RENDER_FIXTURES))
def test_each_render_family_detector_bites(family: str) -> None:
    """A detector that finds nothing on a source built to trip it is dead."""

    findings = find_render_entry_points(RENDER_FIXTURES[family], f"<{family}>")
    assert any(finding.family == family for finding in findings), (
        f"the {family} detector did not fire on its own fixture; "
        f"got {[str(finding) for finding in findings]}"
    )


def test_every_recognised_render_family_has_a_sensitivity_fixture() -> None:
    """No family may be declared without a proof that it is detected."""

    covered = set(RENDER_FIXTURES) | {RenderFamily.unresolvable_import}
    assert covered == ALL_RENDER_FAMILIES, (
        "families without a sensitivity fixture: "
        f"{sorted(ALL_RENDER_FAMILIES - covered)}"
    )


UNRESOLVABLE_FIXTURES: dict[str, str] = {
    "star_import": "from tests.helpers import *\n",
    "dunder_import": "mod = __import__('tests.' + name)\n",
    "dynamic_import_module": (
        "import importlib\nmod = importlib.import_module('tests.' + suffix)\n"
    ),
    "computed_pytest_plugins": "pytest_plugins = [f'tests.{name}']\n",
}


@pytest.mark.parametrize("case", sorted(UNRESOLVABLE_FIXTURES))
def test_each_unresolvable_import_form_is_reported(case: str) -> None:
    findings = find_unresolvable_imports(UNRESOLVABLE_FIXTURES[case], f"<{case}>")
    assert findings, f"the {case} form was silently ignored rather than reported"
    assert all(
        finding.family == RenderFamily.unresolvable_import for finding in findings
    )


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nhttpx.AsyncClient(base_url='https://example.test')\n",
        "import importlib\nmod = importlib.import_module('tests.helpers')\n",
        "pytest_plugins = ['tests.fixtures.huawei']\n",
        "from tests.helpers import build_account\n",
    ],
)
def test_benign_forms_are_not_reported(source: str) -> None:
    """The detectors must not fire on the resolvable, non-render forms.

    Without this, a detector that reported everything would pass every
    sensitivity fixture above while making the real-closure assertions
    impossible to keep green for reasons unrelated to isolation.
    """

    assert not find_render_entry_points(source, "<benign>")
    assert not find_unresolvable_imports(source, "<benign>")


def test_the_closure_walker_follows_pytest_plugins_and_dynamic_imports(
    tmp_path: Path,
) -> None:
    """Both supported dynamic forms must actually extend the closure."""

    from tests.architecture import postgresql_lane_lib

    package = TESTS_ROOT / "architecture"
    entry = tmp_path / "entry.py"
    entry.write_text(
        "pytest_plugins = ['tests.architecture.postgresql_lane_lib']\n"
        "import importlib\n"
        "importlib.import_module('tests.architecture.postgresql_lane_lib')\n",
        encoding="utf-8",
    )
    closure = build_closure([entry])
    assert Path(postgresql_lane_lib.__file__) in closure, (
        f"the walker did not follow a dynamic import into {package}"
    )
