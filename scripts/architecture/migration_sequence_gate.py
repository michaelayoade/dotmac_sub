"""Refuse a migration whose parent is not the base branch's current head.

A migration's ``down_revision`` is chosen when a branch is created and validated
when it merges. With parallel branches that gap is days, so the head named at
authoring time is routinely stale by merge time — and a stale parent forks the
chain. Between 2026-08-06 and 2026-08-07 that produced five numbers claimed by
two or three pull requests each, seven hand renumbers, one orphaned stacked
child surfacing as ``KeyError`` at revision-map load, and one stacked merge
duplicating its parent's migrations.

``scripts/new_migration.py`` reads the head at *authoring* time, which narrows
the window without closing it. This gate closes it: it compares the branch
against its merge base and refuses when the new revisions do not chain onto the
base branch's head. The author rebases once, deterministically, instead of the
collision being discovered after both branches have merged.

See ``docs/adr/0008-migration-sequence-ownership.md``. This gate is the single
named owner of "the deployed schema has one coherent order"; behaviour tests
must not re-derive it.

Usage:
    python scripts/architecture/migration_sequence_gate.py
    python scripts/architecture/migration_sequence_gate.py --base origin/main
    python scripts/architecture/migration_sequence_gate.py --report-only
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

if __package__ in (None, ""):
    from migration_preservation import (
        FIELD_LIFECYCLE_PRESERVATION,
        PreservationDeclaration,
        PreservedBridge,
    )
else:
    from scripts.architecture.migration_preservation import (
        FIELD_LIFECYCLE_PRESERVATION,
        PreservationDeclaration,
        PreservedBridge,
    )

PRESERVATION_SHA256 = "0cc02e35b5518903a983bf2ec669c55415843cd069af9e48b671baad41fc90de"

VERSIONS_DIR = "alembic/versions"


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode("utf-8")


def _parse(source: str) -> tuple[str, tuple[str, ...]]:
    """Read literal revision metadata without executing migration code."""
    metadata: dict[str, object] = {}
    try:
        tree = ast.parse(source)
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            for target in targets:
                if not isinstance(target, ast.Name) or target.id not in {
                    "revision",
                    "down_revision",
                }:
                    continue
                if target.id in metadata:
                    raise MigrationInventoryError(f"Repeated {target.id} metadata.")
                if statement.value is None:
                    raise MigrationInventoryError(
                        f"Missing value for {target.id} metadata."
                    )
                metadata[target.id] = ast.literal_eval(statement.value)
    except (SyntaxError, ValueError, TypeError) as error:
        raise MigrationInventoryError(
            "Migration metadata must be literal and parseable."
        ) from error
    revision = metadata.get("revision")
    down = metadata.get("down_revision")
    if not isinstance(revision, str) or not revision or "down_revision" not in metadata:
        raise MigrationInventoryError(
            "Migration must declare a literal revision and down_revision."
        )
    if down is None:
        return revision, ()
    if isinstance(down, str) and down:
        return revision, (down,)
    if (
        isinstance(down, (tuple, list))
        and down
        and all(isinstance(parent, str) and parent for parent in down)
    ):
        parents = tuple(parent for parent in down if isinstance(parent, str))
        if len(parents) == len(set(parents)):
            return revision, parents
    raise MigrationInventoryError(f"Invalid down_revision metadata for {revision}.")


@dataclass(frozen=True)
class MigrationSource:
    """A host migration, retaining its bytes for immutability verification."""

    path: str
    revision: str
    parents: tuple[str, ...]
    source: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source).hexdigest()


class MigrationInventoryError(Exception):
    """A malformed or ambiguous host migration inventory."""


def _migration(path: str, source: bytes) -> MigrationSource:
    revision, parents = _parse(source.decode("utf-8"))
    return MigrationSource(path, revision, parents, source)


def _inventory_at(ref: str) -> tuple[MigrationSource, ...]:
    listing = _git("ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR)
    return tuple(
        _migration(path, _git_bytes("show", f"{ref}:{path}"))
        for path in listing.splitlines()
        if path.endswith(".py") and not path.endswith("__init__.py")
    )


def _inventory_in_tree(root: Path) -> tuple[MigrationSource, ...]:
    return tuple(
        _migration(path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted((root / VERSIONS_DIR).glob("*.py"))
        if path.name != "__init__.py"
    )


def _index(inventory: tuple[MigrationSource, ...]) -> dict[str, MigrationSource]:
    # Do not build a dictionary until duplicates have been refused.
    seen: set[str] = set()
    for migration in inventory:
        if migration.revision in seen:
            raise MigrationInventoryError(
                f"Duplicate revision ID: {migration.revision}."
            )
        seen.add(migration.revision)
    return {migration.revision: migration for migration in inventory}


def _graph_failures(revisions: dict[str, MigrationSource]) -> list[str]:
    failures = [
        f"{migration.revision} names missing parent {parent}."
        for migration in revisions.values()
        for parent in migration.parents
        if parent not in revisions
    ]
    try:
        tuple(
            TopologicalSorter(
                {r: m.parents for r, m in revisions.items()}
            ).static_order()
        )
    except CycleError:
        failures.append("Host migration graph contains a cycle.")
    return failures


def _heads(revisions: dict[str, MigrationSource]) -> set[str]:
    parents = {p for migration in revisions.values() for p in migration.parents}
    return set(revisions) - parents


def _immutable_failures(
    base: dict[str, MigrationSource], branch: dict[str, MigrationSource]
) -> list[str]:
    return [
        f"Base migration {migration.path} was removed or altered."
        for revision, migration in base.items()
        if revision not in branch or branch[revision] != migration
    ]


def _declaration_digest(declaration: PreservationDeclaration) -> str:
    """Canonical evidence encoding, independent of Python source formatting."""
    payload = {
        "adr_path": declaration.adr_path,
        "base_commit": declaration.base_commit,
        "amendment_heading": declaration.amendment_heading,
        "published_revisions": list(declaration.published_revisions),
        "bridges": [(b.revision, sorted(b.parents)) for b in declaration.bridges],
        "sources": [(s.name, s.manifest_sha256) for s in declaration.sources],
        "revisions": [
            (r.revision, sorted(r.parents), r.path, r.sha256, r.original_path)
            for r in declaration.revisions
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _inert_bridge(source: bytes, expected: PreservedBridge) -> bool:
    """Accept only literal metadata, safe typing imports and inert functions."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    def inert(statement: ast.stmt) -> bool:
        return isinstance(statement, ast.Pass) or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )

    functions: set[str] = set()
    metadata: set[str] = set()
    for statement in tree.body:
        if inert(statement):
            continue
        if isinstance(statement, ast.ImportFrom):
            permitted = {
                "__future__": {"annotations"},
                "collections.abc": {"Sequence"},
            }
            if (
                statement.level
                or statement.module not in permitted
                or any(
                    alias.name not in permitted[statement.module]
                    or alias.asname is not None
                    for alias in statement.names
                )
            ):
                return False
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                return False
            if targets[0].id not in {
                "revision",
                "down_revision",
                "branch_labels",
                "depends_on",
            }:
                return False
            if targets[0].id in metadata:
                return False
            metadata.add(targets[0].id)
            # Annotations can also execute; accept only names and type syntax.
            if isinstance(statement, ast.AnnAssign) and any(
                not isinstance(
                    node,
                    (
                        ast.Name,
                        ast.Load,
                        ast.Subscript,
                        ast.Tuple,
                        ast.BinOp,
                        ast.BitOr,
                        ast.Constant,
                    ),
                )
                for node in ast.walk(statement.annotation)
            ):
                return False
            if statement.value is None:
                return False
            try:
                value: object = ast.literal_eval(statement.value)
                if (
                    targets[0].id in {"branch_labels", "depends_on"}
                    and value is not None
                ):
                    return False
            except (ValueError, TypeError):
                return False
        elif isinstance(statement, ast.FunctionDef):
            if (
                statement.name not in {"upgrade", "downgrade"}
                or statement.name in functions
                or statement.decorator_list
            ):
                return False
            args = statement.args
            if (
                args.posonlyargs
                or args.args
                or args.vararg
                or args.kwonlyargs
                or args.kwarg
                or args.defaults
                or args.kw_defaults
            ):
                return False
            if statement.returns is not None and not (
                isinstance(statement.returns, ast.Constant)
                and statement.returns.value is None
            ):
                return False
            if not all(inert(child) for child in statement.body):
                return False
            functions.add(statement.name)
        else:
            return False
    try:
        revision, parents = _parse(source.decode("utf-8"))
    except (MigrationInventoryError, UnicodeError):
        return False
    return (
        functions == {"upgrade", "downgrade"}
        and metadata == {"revision", "down_revision", "branch_labels", "depends_on"}
        and revision == expected.revision
        and set(parents) == set(expected.parents)
    )


def _preservation_failures(
    base_commit: str,
    merge_base: str,
    root: Path,
    current: dict[str, MigrationSource],
    branch: dict[str, MigrationSource],
    added: dict[str, MigrationSource],
) -> list[str]:
    declaration = FIELD_LIFECYCLE_PRESERVATION
    if declaration is None or _declaration_digest(declaration) != PRESERVATION_SHA256:
        return ["Missing or altered ADR 0030 preservation declaration/provenance."]
    if base_commit != declaration.base_commit or merge_base != declaration.base_commit:
        return ["ADR 0030 preservation requires its exact declared base commit."]
    try:
        approval = (root / declaration.adr_path).read_text(encoding="utf-8")
    except OSError:
        return ["ADR 0030 acceptance evidence is missing."]
    statuses = re.findall(r"^Status: ([^\n]+)$", approval, re.M)
    if len(statuses) != 1 or not re.fullmatch(
        r"accepted(?:\s*[—–-].*)?", statuses[0], re.I
    ):
        return ["ADR 0030 must be accepted before preservation admission."]
    headings = [
        index
        for index, line in enumerate(approval.splitlines())
        if line == declaration.amendment_heading
    ]
    if len(headings) != 1:
        return [
            "ADR 0030 router-health amendment acceptance evidence is missing or ambiguous."
        ]
    section_lines = approval.splitlines()[headings[0] + 1 :]
    end = next(
        (index for index, line in enumerate(section_lines) if line.startswith("## ")),
        len(section_lines),
    )
    amendment_statuses = re.findall(
        r"^Amendment status: ([^\n]+)$", "\n".join(section_lines[:end]), re.M
    )
    if len(amendment_statuses) != 1 or not re.fullmatch(
        r"accepted(?:\s*[—–-].*)?", amendment_statuses[0], re.I
    ):
        return ["ADR 0030 router-health amendment must be separately accepted."]
    declared = {r.revision: r for r in declaration.revisions}
    published = set(declaration.published_revisions)
    if set(added) != set(declared) - published or not published <= set(current):
        return ["ADR 0030 requires the exact declared revision inventory."]
    failures: list[str] = []
    for revision, expected in declared.items():
        actual = branch.get(revision)
        if actual is None or (
            actual.path != expected.path
            or set(actual.parents) != set(expected.parents)
            or actual.sha256 != expected.sha256
        ):
            failures.append(f"ADR 0030 inventory/hash/parents mismatch: {revision}.")
    for expected_bridge in declaration.bridges:
        bridge = branch.get(expected_bridge.revision)
        if bridge is None or not _inert_bridge(bridge.source, expected_bridge):
            failures.append(
                f"ADR 0030 convergence bridge must be inert: {expected_bridge.revision}."
            )
    return failures


def check(base: str, root: Path) -> list[str]:
    """Return human-readable failures; empty means the branch is admissible."""
    try:
        merge_base = _git("merge-base", base, "HEAD").strip()
        base_commit = _git("rev-parse", f"{base}^{{commit}}").strip()
        base_revisions = _index(_inventory_at(merge_base))
        current = (
            base_revisions
            if base_commit == merge_base
            else _index(_inventory_at(base_commit))
        )
        branch = _index(_inventory_in_tree(root))
    except subprocess.CalledProcessError:
        return [f"Cannot resolve merge base or migration inventory against {base!r}."]
    except (MigrationInventoryError, UnicodeError, OSError) as error:
        return [str(error)]

    failures = _graph_failures(base_revisions) + _graph_failures(current)
    failures += _immutable_failures(base_revisions, branch)
    failures += _graph_failures(branch)
    if failures:
        return failures
    added = {rev: m for rev, m in branch.items() if rev not in base_revisions}
    current_heads = _heads(current)
    if len(current_heads) != 1:
        return [
            f"{base} is not single-headed ({sorted(current_heads)}). Repair the base branch before adding a migration."
        ]
    if not added:
        return []
    # A branch cannot overwrite a migration added to the current base meanwhile.
    for revision in set(added) & set(current):
        if added[revision] != current[revision]:
            failures.append(f"Current base migration {revision} was altered.")
    combined = {**current, **added}
    failures += _graph_failures(combined)
    resulting = _heads(combined)
    if len(resulting) != 1:
        failures.append(
            f"Merging would leave {len(resulting)} heads: {sorted(resulting)}."
        )
    if failures:
        return failures

    if "671_assignment_generation" in added:
        return _preservation_failures(
            base_commit, merge_base, root, current, branch, added
        )

    head = next(iter(current_heads))
    roots = [r for r, m in added.items() if not any(p in added for p in m.parents)]
    for revision in roots:
        if added[revision].parents != (head,):
            failures.append(
                f"{revision} has down_revision {added[revision].parents!r} but {base} is now at {head!r}.\n"
                f"    Rebase onto {base} and repoint it — merging as-is forks the chain."
            )
    if len(roots) != 1:
        failures.append(
            f"This branch adds {len(roots)} independent migration roots ({sorted(roots)}). A branch must contribute one linear segment."
        )
    for revision, migration in added.items():
        if revision not in roots and (
            len(migration.parents) != 1 or migration.parents[0] not in added
        ):
            failures.append(f"{revision} does not belong to one linear segment.")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="base branch ref")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="print findings and exit 0 (shadow phase, see ADR 0008)",
    )
    args = parser.parse_args(argv)

    root = Path(_git("rev-parse", "--show-toplevel").strip())
    failures = check(args.base, root)

    if not failures:
        print(f"migration sequence gate: OK against {args.base}")
        return 0

    print(f"migration sequence gate: {len(failures)} finding(s)\n")
    for failure in failures:
        print(f"  - {failure}")
    print(
        "\nSee docs/adr/0008-migration-sequence-ownership.md. "
        "`make new-migration slug=...` allocates from the current head."
    )

    if args.report_only:
        print("\n(report-only: not failing the build — ADR 0008 shadow phase)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
