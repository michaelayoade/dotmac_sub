"""Behaviour and sensitivity proofs for the PostgreSQL change classifier.

ADR-0018 asks a guard to carry a sensitivity proof: a test that a rule fired is
worth nothing unless it would also fail if that rule were removed.  This
classifier defaults to "PostgreSQL required", so asserting ``required is True``
proves nothing at all -- the fail-closed default would answer identically with
every real rule deleted.  Two devices close that gap:

- every requirement assertion names the exact
  :class:`ClassificationReason` that must have decided the path; and
- the exemption rules live in module-level tables, so a test can remove one and
  assert that the verdict actually changes.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from scripts.ci.classify_postgresql_changes import (
    EXEMPT_REASONS,
    ISOLATED_TEST_PACKAGES,
    NON_DATABASE_TREES,
    ClassificationReason,
    PathClassification,
    classify_path,
    classify_postgresql_changes,
    main,
    render_explanation,
)

#: One path per shape the classifier recognises, used by the totality and
#: dead-exemption proofs below.
_EVERY_SHAPE = (
    "docs/adr/0001.md",
    "README.md",
    "docs/diagram.svg",
    "templates/admin/dashboard.html",
    "static/css/main.css",
    "mobile/lib/main.dart",
    "tests/architecture/test_x.py",
    "tests/conftest.py",
    "tests/mocks.py",
    "tests/fixtures/huawei/sample.xml",
    "tests/integration/test_flow_quotes.py",
    "alembic/versions/601_x.py",
    "app/services/billing.py",
    "pyproject.toml",
)


def _reason(path: str) -> ClassificationReason:
    return classify_path(PurePosixPath(path))


# --------------------------------------------------------------------------
# Paths that must require PostgreSQL, each pinned to the rule that decides it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The five root-level helpers the integration suite imports today.
        ("tests/staff_identity_fixtures.py", ClassificationReason.test_shared_module),
        ("tests/referral_program_testkit.py", ClassificationReason.test_shared_module),
        ("tests/prepaid_funding_helpers.py", ClassificationReason.test_shared_module),
        ("tests/test_crm_ticket_pull.py", ClassificationReason.test_shared_module),
        (
            "tests/test_integration_whatsapp_capability.py",
            ClassificationReason.test_shared_module,
        ),
        # Any other root-level module: reachable by the same mechanism.
        ("tests/mocks.py", ClassificationReason.test_shared_module),
        # Shared fixture data is not code, but the lane can still read it.
        ("tests/fixtures/huawei/sample.xml", ClassificationReason.test_shared_module),
        # conftest at every depth, including inside an otherwise exempt package.
        ("tests/conftest.py", ClassificationReason.test_conftest),
        ("tests/integration/conftest.py", ClassificationReason.test_conftest),
        ("tests/services/conftest.py", ClassificationReason.test_conftest),
        ("tests/architecture/conftest.py", ClassificationReason.test_conftest),
        # The lane itself.
        (
            "tests/integration/test_flow_quotes.py",
            ClassificationReason.integration_test,
        ),
        # Schema and application source.
        ("alembic/versions/601_something.py", ClassificationReason.migration_source),
        ("alembic/env.py", ClassificationReason.migration_source),
        ("app/services/billing.py", ClassificationReason.application_source),
        # Unknown paths: no rule claims them, so they fail closed.
        (
            "scripts/ci/select_integration_shard.py",
            ClassificationReason.unclassified_path,
        ),
        ("pyproject.toml", ClassificationReason.unclassified_path),
        ("poetry.lock", ClassificationReason.unclassified_path),
        ("Makefile", ClassificationReason.unclassified_path),
        ("Dockerfile", ClassificationReason.unclassified_path),
        (".github/workflows/ci.yml", ClassificationReason.unclassified_path),
        (
            "tests/a_package_nobody_declared/test_x.py",
            ClassificationReason.test_shared_module,
        ),
        (
            "a_top_level_tree_nobody_declared/x.py",
            ClassificationReason.unclassified_path,
        ),
    ],
)
def test_path_requires_postgresql_for_the_named_reason(
    path: str, expected: ClassificationReason
) -> None:
    reason = _reason(path)
    assert reason is expected
    assert classify_postgresql_changes((path,)).required is True


# --------------------------------------------------------------------------
# Paths that may skip PostgreSQL, each pinned to the exemption that allows it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "docs/adr/0008-migration-sequence-ownership.md",
            ClassificationReason.documentation,
        ),
        ("README.md", ClassificationReason.documentation),
        ("app/services/README.md", ClassificationReason.documentation),
        ("docs/diagram.svg", ClassificationReason.documentation),
        ("templates/admin/dashboard.html", ClassificationReason.presentation_template),
        ("static/css/main.css", ClassificationReason.static_asset),
        ("mobile/lib/main.dart", ClassificationReason.mobile_client),
        (
            "tests/architecture/test_sot_registry.py",
            ClassificationReason.isolated_test_package,
        ),
        ("tests/unit/test_money.py", ClassificationReason.isolated_test_package),
        ("tests/services/test_quotes.py", ClassificationReason.isolated_test_package),
        (
            "tests/scripts/test_new_migration.py",
            ClassificationReason.isolated_test_package,
        ),
        ("tests/js/app.test.js", ClassificationReason.isolated_test_package),
        ("tests/playwright/test_login.py", ClassificationReason.isolated_test_package),
    ],
)
def test_path_is_exempt_for_the_named_reason(
    path: str, expected: ClassificationReason
) -> None:
    reason = _reason(path)
    assert reason is expected
    assert classify_postgresql_changes((path,)).required is False


# --------------------------------------------------------------------------
# Sensitivity: remove a rule, and the classification must change
# --------------------------------------------------------------------------


def test_the_isolated_package_allowlist_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the allowlist, an exempt test package must fail closed.

    If this passes with the allowlist emptied, the exemption above was being
    granted by something other than the rule it claims to test.
    """

    path = "tests/architecture/test_sot_registry.py"
    assert classify_postgresql_changes((path,)).required is False
    monkeypatch.setattr(
        "scripts.ci.classify_postgresql_changes.ISOLATED_TEST_PACKAGES", frozenset()
    )
    decision = classify_postgresql_changes((path,))
    assert decision.required is True
    assert decision.reasons == (ClassificationReason.test_shared_module,)


@pytest.mark.parametrize(
    ("tree", "path"),
    [
        ("docs", "docs/diagram.svg"),
        ("templates", "templates/admin/dashboard.html"),
        ("static", "static/css/main.css"),
        ("mobile", "mobile/lib/main.dart"),
    ],
)
def test_each_non_database_tree_exemption_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch, tree: str, path: str
) -> None:
    assert classify_postgresql_changes((path,)).required is False
    remaining = {key: value for key, value in NON_DATABASE_TREES.items() if key != tree}
    monkeypatch.setattr(
        "scripts.ci.classify_postgresql_changes.NON_DATABASE_TREES", remaining
    )
    decision = classify_postgresql_changes((path,))
    assert decision.required is True
    assert decision.reasons == (ClassificationReason.unclassified_path,)


def test_every_isolated_package_is_individually_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No entry in the allowlist may be dead weight.

    A name that changes nothing when removed is a name nobody proved, and the
    isolation guard would then be vouching for a package the classifier never
    actually exempts.
    """

    for package in ISOLATED_TEST_PACKAGES:
        path = f"tests/{package}/test_probe.py"
        assert classify_postgresql_changes((path,)).required is False, package
        monkeypatch.setattr(
            "scripts.ci.classify_postgresql_changes.ISOLATED_TEST_PACKAGES",
            ISOLATED_TEST_PACKAGES - {package},
        )
        assert classify_postgresql_changes((path,)).required is True, package
        monkeypatch.undo()


def test_requirement_is_the_absence_of_an_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting configuration must run MORE tests, never fewer.

    This is the property that makes the design fail closed. With the exemption
    set emptied, a path that is exempt today must require PostgreSQL -- so no
    edit to, corruption of, or accidental truncation of that table can ever
    turn the lane off.
    """

    exempt = "docs/adr/0008-migration-sequence-ownership.md"
    assert classify_postgresql_changes((exempt,)).required is False
    monkeypatch.setattr(
        "scripts.ci.classify_postgresql_changes.EXEMPT_REASONS", frozenset()
    )
    assert classify_postgresql_changes((exempt,)).required is True


def test_every_reason_outside_the_exemption_set_requires_postgresql() -> None:
    """A reason added to the enum and forgotten must default to requiring.

    There is no requiring-reason registry to update, so this holds for members
    that do not exist yet -- which is the point of stating it over the whole
    enum rather than over a list of known-requiring names.
    """

    for reason in ClassificationReason:
        classification = PathClassification("some/path", reason)
        assert classification.requires_postgresql is (reason not in EXEMPT_REASONS)


def test_the_exemption_set_is_a_strict_subset_of_the_reasons() -> None:
    """An exemption naming a reason that cannot be produced is dead weight."""

    assert EXEMPT_REASONS < set(ClassificationReason)
    producible = {classify_path(PurePosixPath(path)) for path in _EVERY_SHAPE}
    unreachable = EXEMPT_REASONS - producible
    assert not unreachable, f"exemptions no path can trigger: {sorted(unreachable)}"


# --------------------------------------------------------------------------
# Change-set level behaviour
# --------------------------------------------------------------------------


def test_an_empty_change_set_requires_postgresql() -> None:
    decision = classify_postgresql_changes(())
    assert decision.required is True
    assert decision.reasons == (ClassificationReason.empty_change_set,)


def test_blank_lines_do_not_empty_a_real_change_set() -> None:
    decision = classify_postgresql_changes(("", "  ", "docs/x.md", ""))
    assert decision.required is False
    assert decision.reasons == (ClassificationReason.documentation,)


def test_one_requiring_path_carries_an_otherwise_exempt_change_set() -> None:
    decision = classify_postgresql_changes(
        ("docs/x.md", "templates/y.html", "app/services/billing.py")
    )
    assert decision.required is True
    assert decision.requiring_paths == ("app/services/billing.py",)


def test_the_explanation_names_the_deciding_rule_and_path() -> None:
    decision = classify_postgresql_changes(("docs/x.md", "tests/conftest.py"))
    explanation = render_explanation(decision)
    assert "required" in explanation
    assert "test_conftest: tests/conftest.py" in explanation
    assert "docs/x.md" not in explanation


def test_the_explanation_lists_every_path_when_nothing_requires_postgresql() -> None:
    explanation = render_explanation(classify_postgresql_changes(("docs/x.md",)))
    assert "not required" in explanation
    assert "documentation: docs/x.md" in explanation


# --------------------------------------------------------------------------
# Diff shapes git actually produces
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "required"),
    [
        # Rename with detection off: git lists the old and the new path.
        (("docs/guide.md", "app/services/guide.py"), True),
        # Rename with detection on: git lists the destination only.
        (("app/services/guide.py",), True),
        # Rename entirely within an exempt tree.
        (("docs/old.md", "docs/new.md"), False),
        # Rename OUT of an exempt tree into a requiring one, and back.
        (("templates/x.html", "app/x.py"), True),
        (("app/x.py", "templates/x.html"), True),
        # A deleted path is still listed, and still classified.
        (("alembic/versions/600_removed.py",), True),
        (("docs/removed.md",), False),
        # A deleted root-level test helper must still trigger the lane.
        (("tests/staff_identity_fixtures.py",), True),
    ],
)
def test_git_diff_shapes_are_classified(paths: tuple[str, ...], required: bool) -> None:
    assert classify_postgresql_changes(paths).required is required


def test_every_path_receives_exactly_one_reason() -> None:
    """One path, one deciding rule -- no path may be classified twice or zero times."""

    paths = tuple(_EVERY_SHAPE)
    decision = classify_postgresql_changes(paths)
    assert len(decision.classifications) == len(paths)
    assert tuple(item.path for item in decision.classifications) == paths
    for item in decision.classifications:
        assert isinstance(item.reason, ClassificationReason)


def test_classification_is_total_over_the_real_repository_tree() -> None:
    """No tracked path may raise, and every one must land on a reason."""

    repository_root = Path(__file__).resolve().parents[2]
    tracked = [
        path.relative_to(repository_root).as_posix()
        for path in repository_root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    ]
    assert len(tracked) > 1000, "the repository walk found too little to be meaningful"
    decision = classify_postgresql_changes(tuple(tracked))
    assert len(decision.classifications) == len(tracked)
    assert decision.required is True


# --------------------------------------------------------------------------
# Malformed input fails closed by failing the job
# --------------------------------------------------------------------------


def test_an_unreadable_paths_file_fails_the_job(tmp_path: Path) -> None:
    """Undecodable bytes must raise, not classify as an empty change set.

    The `changes` job runs under `bash -e`, so a non-zero exit fails it, and
    every downstream gate keys on `needs.changes.result == 'success'`. Crashing
    is the fail-closed outcome; returning "nothing changed" would not be.
    """

    paths_file = tmp_path / "changed-paths"
    paths_file.write_bytes(b"app/services/billing.py\n\xff\xfe not utf-8 \n")
    output = tmp_path / "github-output"
    output.touch()
    with pytest.raises(UnicodeDecodeError):
        main(
            [
                "--paths-file",
                str(paths_file),
                "--github-output",
                str(output),
            ]
        )
    assert output.read_text(encoding="utf-8") == ""


def test_a_missing_paths_file_fails_the_job(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.touch()
    with pytest.raises(FileNotFoundError):
        main(
            [
                "--paths-file",
                str(tmp_path / "absent"),
                "--github-output",
                str(output),
            ]
        )
    assert output.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/absolute/outside/repo.py",
        "./app/services/billing.py",
        "app//services//billing.py",
        "app/services/../services/billing.py",
        "a path with spaces.py",
        "unicode/\u00e9\u00e7\u00e0.py",
    ],
)
def test_unusual_path_spellings_fail_closed(path: str) -> None:
    assert classify_postgresql_changes((path,)).required is True


def test_the_cli_writes_the_verdict_for_a_real_change_set(tmp_path: Path) -> None:
    paths_file = tmp_path / "changed-paths"
    paths_file.write_text("docs/a.md\ndocs/b.md\n", encoding="utf-8")
    output = tmp_path / "github-output"
    output.touch()
    main(["--paths-file", str(paths_file), "--github-output", str(output)])
    assert output.read_text(encoding="utf-8") == "postgresql-required=false\n"

    paths_file.write_text("docs/a.md\napp/x.py\n", encoding="utf-8")
    output.write_text("", encoding="utf-8")
    main(["--paths-file", str(paths_file), "--github-output", str(output)])
    assert output.read_text(encoding="utf-8") == "postgresql-required=true\n"
