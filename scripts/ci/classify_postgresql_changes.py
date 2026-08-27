"""Classify whether a change needs PostgreSQL validation, failing closed.

The PostgreSQL lane (`tests/integration/`, run against a database built by the
real Alembic chain) is the only lane this module gates.  Skipping it is a
performance optimisation, so the burden of proof runs one way: a path earns an
exemption by being provably unreachable from that lane, and everything else --
including a path this module has never seen -- requires PostgreSQL.

Every decision carries an exact :class:`ClassificationReason`.  That is what
makes the guard testable in the sense ADR-0018 requires: a sensitivity proof
has to show that the *intended* rule fired, because a test asserting only
``required is True`` would still pass if the rule it covers were deleted and
the fail-closed default answered in its place.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

# Test packages the PostgreSQL lane cannot load.  This is an ENFORCEABLE
# premise, not an assertion of good intent: `test_postgresql_lane_isolation.py`
# fails if anything in the lane's reachable set imports one of these.  A test
# package that is not listed here is not exempt -- it is simply unknown, and
# unknown fails closed.
ISOLATED_TEST_PACKAGES = frozenset(
    {
        "architecture",
        "js",
        "playwright",
        "scripts",
        "services",
        "unit",
    }
)

# Top-level trees with no import path into the PostgreSQL lane.  `templates`
# and `static` are here because nothing the lane loads reaches ANY request or
# render entry point -- test client, httpx ASGI transport, local client wrapper,
# direct Jinja render, or an import of the ASGI application itself.  That is
# proven by `test_postgresql_lane_isolation.py` over the lane's transitive
# import closure, not assumed here.
NON_DATABASE_TREES = {
    "docs": "documentation",
    "templates": "presentation_template",
    "static": "static_asset",
    "mobile": "mobile_client",
}


class ClassificationReason(StrEnum):
    """Exact rule that decided one path, or the whole change set."""

    # --- exempt: provably outside the PostgreSQL lane ------------------------
    documentation = "documentation"
    presentation_template = "presentation_template"
    static_asset = "static_asset"
    mobile_client = "mobile_client"
    isolated_test_package = "isolated_test_package"

    # --- require PostgreSQL --------------------------------------------------
    empty_change_set = "empty_change_set"
    migration_source = "migration_source"
    application_source = "application_source"
    integration_test = "integration_test"
    test_conftest = "test_conftest"
    test_shared_module = "test_shared_module"
    unclassified_path = "unclassified_path"


# The ONLY reasons that permit skipping PostgreSQL. Requirement is the absence
# of an exemption, never the presence of a requiring rule, and the direction is
# the whole point: deleting configuration here can only ever run MORE tests.
#
# A registry of requiring reasons would be fail-open by construction -- a reason
# added to the enum and forgotten here would silently stop triggering the lane,
# and emptying the registry would disable the lane entirely. Inverted, a new
# reason requires PostgreSQL until someone deliberately exempts it, and emptying
# this set requires PostgreSQL for everything.
EXEMPT_REASONS = frozenset(
    {
        ClassificationReason.documentation,
        ClassificationReason.presentation_template,
        ClassificationReason.static_asset,
        ClassificationReason.mobile_client,
        ClassificationReason.isolated_test_package,
    }
)


@dataclass(frozen=True, slots=True)
class PathClassification:
    path: str
    reason: ClassificationReason

    @property
    def requires_postgresql(self) -> bool:
        return self.reason not in EXEMPT_REASONS


@dataclass(frozen=True, slots=True)
class PostgreSQLValidationDecision:
    required: bool
    classifications: tuple[PathClassification, ...]

    @property
    def requiring_paths(self) -> tuple[str, ...]:
        return tuple(
            item.path for item in self.classifications if item.requires_postgresql
        )

    @property
    def reasons(self) -> tuple[ClassificationReason, ...]:
        """Distinct reasons, in first-seen order."""

        seen: dict[ClassificationReason, None] = {}
        for item in self.classifications:
            seen.setdefault(item.reason, None)
        return tuple(seen)


def _classify_test_path(path: PurePosixPath) -> ClassificationReason:
    """Classify a path under ``tests/``.

    Order matters.  A conftest anywhere is shared setup, so it is checked
    before the package allowlist can exempt it.
    """

    if path.name == "conftest.py":
        return ClassificationReason.test_conftest
    parts = path.parts
    if len(parts) == 2:
        # A module directly under `tests/` is shared helper surface: the
        # integration suite imports `tests.staff_identity_fixtures`,
        # `tests.referral_program_testkit`, `tests.prepaid_funding_helpers`,
        # `tests.test_crm_ticket_pull` and
        # `tests.test_integration_whatsapp_capability` today, and nothing stops
        # the next one being added without touching this file.
        return ClassificationReason.test_shared_module
    package = parts[1]
    if package == "integration":
        return ClassificationReason.integration_test
    if package in ISOLATED_TEST_PACKAGES:
        return ClassificationReason.isolated_test_package
    # `tests/fixtures/` and any package added later: unknown, so fail closed.
    return ClassificationReason.test_shared_module


def classify_path(path: PurePosixPath) -> ClassificationReason:
    """Decide one path with the exact rule that applies to it."""

    value = path.as_posix()
    if value.endswith(".md"):
        return ClassificationReason.documentation
    parts = path.parts
    if not parts:
        return ClassificationReason.unclassified_path
    root = parts[0]
    if root == "tests":
        return _classify_test_path(path)
    if root == "alembic":
        return ClassificationReason.migration_source
    if root == "app":
        return ClassificationReason.application_source
    tree_reason = NON_DATABASE_TREES.get(root)
    if tree_reason is not None and len(parts) > 1:
        return ClassificationReason(tree_reason)
    # Anything else -- `scripts/`, `pyproject.toml`, `Makefile`, `Dockerfile`,
    # `.github/`, a top-level tree that does not exist yet -- fails closed.
    return ClassificationReason.unclassified_path


def classify_postgresql_changes(
    changed_paths: tuple[str, ...],
) -> PostgreSQLValidationDecision:
    """Fail closed for empty or backend-affecting change sets."""

    normalized = tuple(
        PurePosixPath(path.strip()).as_posix() for path in changed_paths if path.strip()
    )
    if not normalized:
        return PostgreSQLValidationDecision(
            required=True,
            classifications=(
                PathClassification("", ClassificationReason.empty_change_set),
            ),
        )
    classifications = tuple(
        PathClassification(path, classify_path(PurePosixPath(path)))
        for path in normalized
    )
    return PostgreSQLValidationDecision(
        required=any(item.requires_postgresql for item in classifications),
        classifications=classifications,
    )


def render_explanation(decision: PostgreSQLValidationDecision) -> str:
    """Human-readable account of which rule decided what."""

    verdict = "required" if decision.required else "not required"
    lines = [f"PostgreSQL validation {verdict}."]
    if decision.required:
        deciding = [
            item for item in decision.classifications if item.requires_postgresql
        ]
        lines.append(f"{len(deciding)} path(s) require it:")
        for item in deciding[:20]:
            lines.append(f"  {item.reason.value}: {item.path}")
        if len(deciding) > 20:
            lines.append(f"  ... and {len(deciding) - 20} more")
    else:
        lines.append("Every changed path is provably outside the PostgreSQL lane:")
        for item in decision.classifications[:20]:
            lines.append(f"  {item.reason.value}: {item.path}")
        if len(decision.classifications) > 20:
            lines.append(f"  ... and {len(decision.classifications) - 20} more")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    decision = classify_postgresql_changes(
        tuple(args.paths_file.read_text(encoding="utf-8").splitlines())
    )
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"postgresql-required={str(decision.required).lower()}\n")
    explanation = render_explanation(decision)
    print(explanation)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"### Change classification\n\n```\n{explanation}\n```\n")


if __name__ == "__main__":
    main()
