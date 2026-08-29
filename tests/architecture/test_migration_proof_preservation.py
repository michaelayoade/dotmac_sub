"""Cloning may not consume the last genuine fresh-chain proof.

`tests/integration/conftest.py` lets a test take a byte-identical copy of a
schema the real Alembic chain already built, instead of replaying that chain
itself. For a test whose subject is BEHAVIOUR on a migrated schema this changes
nothing about the evidence and removes ~50 seconds of replay.

It is not free everywhere. A module that also makes a claim ABOUT MIGRATING --
that a fresh chain produces some constraint, or that an existing database gains
it between two named revisions -- needs at least one test where the chain
actually runs, or the claim's subject has quietly moved into a fixture that
asserts nothing.

This guard states that rule over the whole integration package, so the next
module to adopt cloning inherits it without anyone remembering to.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = REPOSITORY_ROOT / "tests" / "integration"

CLONING_FIXTURES = frozenset({"cloned_database"})

#: Fixtures that identify a test which REPLAYS the chain from an empty
#: database. Some create the empty database and let the test drive Alembic;
#: others also perform the upgrade. Named rather than pattern-matched: a new
#: fixture has to be added here deliberately, and the conversion arithmetic
#: below makes every entry load-bearing.
CHAIN_REPLAYING_FIXTURES = frozenset(
    {
        "fresh_migration_database",
        "freshly_migrated_database",
        "isolated_migration_database",
        "isolated_database",
    }
)


def _fixture_parameters(tree: ast.Module) -> set[str]:
    """Every fixture name any test function in the module requests."""

    requested: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                continue
            arguments = node.args
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ):
                requested.add(argument.arg)
    return requested


def _runs_alembic_at_a_named_revision(tree: ast.Module) -> bool:
    """True when the module drives Alembic at anything other than `heads`.

    Upgrading a clone to `heads` is a no-op restatement of what the template
    already is. Naming a specific revision -- a predecessor, a candidate, a
    downgrade target -- is a claim about the migration path itself.
    """

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else function.id
            if isinstance(function, ast.Name)
            else None
        )
        if name not in {"upgrade", "downgrade"}:
            continue
        # Only the REVISION argument decides this, not the Config that precedes
        # it. Inspecting every argument made `command.upgrade(config, "heads")`
        # report a named revision, because `config` is not the string "heads".
        revision = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "revision"),
            node.args[-1] if len(node.args) > 1 else None,
        )
        if revision is None:
            continue
        if isinstance(revision, ast.Constant) and revision.value == "heads":
            continue
        return True
    return False


def _modules() -> dict[Path, ast.Module]:
    parsed: dict[Path, ast.Module] = {}
    for path in sorted(INTEGRATION_ROOT.rglob("test_*.py")):
        parsed[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return parsed


def test_a_cloning_module_that_claims_a_migration_path_keeps_a_fresh_chain_proof() -> (
    None
):
    offenders: list[str] = []
    for path, tree in _modules().items():
        requested = _fixture_parameters(tree)
        if not requested & CLONING_FIXTURES:
            continue
        if not _runs_alembic_at_a_named_revision(tree):
            continue
        if requested & CHAIN_REPLAYING_FIXTURES:
            continue
        offenders.append(path.relative_to(REPOSITORY_ROOT).as_posix())
    assert not offenders, (
        "These modules clone a migrated template AND assert something about "
        "the migration path, but no test in them replays the chain -- so the "
        "migration claim rests on a fixture rather than on a migration: "
        f"{sorted(offenders)}."
    )


def test_the_guard_has_something_to_check() -> None:
    """Non-vacuity: with no module cloning, the check above passes emptily."""

    cloning = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path, tree in _modules().items()
        if _fixture_parameters(tree) & CLONING_FIXTURES
    ]
    assert cloning, (
        "No integration module uses the cloning fixtures, so this guard proves "
        "nothing. Retire it, or fix whatever removed the last consumer."
    )


def test_the_named_revision_detector_distinguishes_heads_from_a_revision() -> None:
    """Sensitivity: a detector that answered the same either way is useless."""

    heads_only = ast.parse('command.upgrade(config, "heads")')
    named = ast.parse('command.upgrade(config, "467_sla_policy_versions")')
    downgrade = ast.parse("command.downgrade(config, PREDECESSOR)")
    assert _runs_alembic_at_a_named_revision(heads_only) is False
    assert _runs_alembic_at_a_named_revision(named) is True
    assert _runs_alembic_at_a_named_revision(downgrade) is True


def test_the_sla_module_still_proves_both_migration_directions() -> None:
    """The file this mechanism was built for keeps its named proofs.

    Its docstring claims two: fresh acceptance (baseline to head builds the
    constraints) and incremental acceptance (the real predecessor gains them).
    Both must still run the chain.
    """

    path = INTEGRATION_ROOT / "test_sla_policy_versions_postgres.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    fresh = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        and {argument.arg for argument in node.args.args} & CHAIN_REPLAYING_FIXTURES
    }
    assert "test_head_builds_the_table_with_its_migration_only_constraints" in fresh
    assert "test_existing_production_database_gains_the_constraints" in fresh
    assert "test_existing_policy_table_gains_family_identity_constraints" in fresh


def test_each_converted_module_pins_its_fresh_and_cloned_proof_arithmetic() -> None:
    """A behaviour test drifting back to fresh replay is a CI regression.

    Exact counts also make every chain-replaying fixture name above
    load-bearing: removing ``fresh_migration_database`` changes the measured
    split for both new conversion files and fails this guard.
    """

    expected = {
        "test_sla_policy_versions_postgres.py": (16, 3, 13),
        "test_migrations_512_setting_value_type.py": (4, 2, 2),
        "test_lifecycle_evidence_authority_migration.py": (5, 1, 4),
    }
    for name, wanted in expected.items():
        path = INTEGRATION_ROOT / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        tests = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        fresh = [
            node
            for node in tests
            if {argument.arg for argument in node.args.args} & CHAIN_REPLAYING_FIXTURES
        ]
        cloning = [
            node
            for node in tests
            if {argument.arg for argument in node.args.args} & CLONING_FIXTURES
        ]
        actual = (len(tests), len(fresh), len(cloning))
        assert actual == wanted, f"{name}: expected {wanted}, found {actual}"
        assert not {node.name for node in fresh} & {node.name for node in cloning}, (
            f"{name}: one test cannot be both a fresh replay and a clone"
        )


def test_each_converted_module_clones_one_shared_head_template() -> None:
    """A second target would replay another full chain and erase the saving."""

    for name in (
        "test_sla_policy_versions_postgres.py",
        "test_migrations_512_setting_value_type.py",
        "test_lifecycle_evidence_authority_migration.py",
    ):
        path = INTEGRATION_ROOT / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        targets = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "cloned_database"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert targets == {"heads"}, (
            f"{name}: clones span several templates: {sorted(targets)}"
        )
