"""Bounded ADR 0030 admission tests through the existing sequence owner.

These are static owner tests, not database migration acceptance evidence.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.architecture import migration_sequence_gate as gate
from scripts.architecture.migration_preservation import FIELD_LIFECYCLE_PRESERVATION

REPOSITORY = Path(__file__).resolve().parents[2]
DECLARATION = FIELD_LIFECYCLE_PRESERVATION
BASE = DECLARATION.base_commit
PUBLISHED = "671_customer_connection_type_definitions"
BRIDGE = "683_field_lifecycle_connection_types_merge"
NEW_BRIDGE = "686_field_lifecycle_router_health_merge"
PUBLISHED_ROUTER = "672_router_health_reconciler"
ACCEPTED_APPROVAL = (
    "# ADR 0030\n\nStatus: accepted\n\n"
    + DECLARATION.amendment_heading
    + "\n\nAmendment status: accepted\n"
)


def migration(revision: str, parents: tuple[str, ...]) -> gate.MigrationSource:
    down: str | tuple[str, ...] | None = (
        parents[0] if len(parents) == 1 else parents or None
    )
    return gate.MigrationSource(
        f"alembic/versions/{revision}.py",
        revision,
        parents,
        f"revision = {revision!r}\ndown_revision = {down!r}\n".encode(),
    )


class PreservedMigrationAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / gate.VERSIONS_DIR).mkdir(parents=True)
        self.approval = self.root / DECLARATION.adr_path
        self.approval.parent.mkdir(parents=True)
        self.approval.write_text(ACCEPTED_APPROVAL)
        ancestor = migration("670_user_credential_version", ())
        self.write(ancestor)
        for declaration in DECLARATION.revisions:
            path = self.root / declaration.path
            path.write_bytes((REPOSITORY / declaration.path).read_bytes())
        self.base = (
            ancestor,
            *(
                item
                for item in gate._inventory_in_tree(self.root)
                if item.revision in DECLARATION.published_revisions
            ),
        )
        self.base_commit = BASE
        self.merge_base = BASE
        self.current: tuple[gate.MigrationSource, ...] = self.base
        self.addCleanup(patch.stopall)
        patch.object(gate, "_git", side_effect=self.git).start()
        patch.object(gate, "_inventory_at", side_effect=self.inventory).start()

    def git(self, *args: str) -> str:
        return self.merge_base if args[0] == "merge-base" else self.base_commit

    def inventory(self, ref: str) -> tuple[gate.MigrationSource, ...]:
        return self.base if ref == self.merge_base else self.current

    def write(self, item: gate.MigrationSource) -> None:
        (self.root / item.path).write_bytes(item.source)

    def findings(self) -> list[str]:
        return gate.check(BASE, self.root)

    def assert_refused(self, text: str) -> None:
        self.assertIn(text, "\n".join(self.findings()))

    def test_exact_declared_graph_is_admitted(self) -> None:
        self.assertEqual([], self.findings())

    def test_external_module_heads_stay_outside_host_inventory(self) -> None:
        external = self.root / "modules/network/versions"
        external.mkdir(parents=True)
        (external / "external.py").write_bytes(migration("module_head", ()).source)
        self.assertEqual([], self.findings())

    def test_missing_declaration_is_refused(self) -> None:
        patch.object(gate, "FIELD_LIFECYCLE_PRESERVATION", None).start()
        self.assert_refused("Missing or altered")

    def test_altered_declaration_inventory_is_refused(self) -> None:
        patch.object(
            gate,
            "FIELD_LIFECYCLE_PRESERVATION",
            replace(DECLARATION, revisions=DECLARATION.revisions[:-1]),
        ).start()
        self.assert_refused("Missing or altered")

    def test_altered_source_provenance_is_refused(self) -> None:
        source = replace(DECLARATION.sources[0], manifest_sha256="0" * 64)
        patch.object(
            gate,
            "FIELD_LIFECYCLE_PRESERVATION",
            replace(DECLARATION, sources=(source, *DECLARATION.sources[1:])),
        ).start()
        self.assert_refused("Missing or altered")

    def test_proposed_approval_fails_closed(self) -> None:
        self.approval.write_text("Status: proposed — pending approval\n")
        self.assert_refused("must be accepted")

    def test_ambiguous_approval_fails_closed(self) -> None:
        self.approval.write_text("Status: proposed\nStatus: accepted\n")
        self.assert_refused("must be accepted")

    def test_missing_approval_fails_closed(self) -> None:
        self.approval.unlink()
        self.assert_refused("acceptance evidence is missing")

    def test_wrong_base_commit_is_refused(self) -> None:
        self.base_commit = "0" * 40
        self.assert_refused("exact declared base")

    def test_wrong_merge_base_is_refused(self) -> None:
        self.merge_base = "1" * 40
        self.assert_refused("exact declared base")

    def test_altered_migration_bytes_are_refused(self) -> None:
        path = self.root / "alembic/versions/685_assignment_generation.py"
        path.write_bytes(path.read_bytes() + b"\n# altered\n")
        self.assert_refused("inventory/hash/parents mismatch")

    def test_wrong_filename_is_refused(self) -> None:
        path = self.root / "alembic/versions/685_assignment_generation.py"
        path.rename(path.with_name("671_assignment_generation.py"))
        self.assert_refused("inventory/hash/parents mismatch")

    def test_changed_parent_is_refused(self) -> None:
        path = self.root / "alembic/versions/687_field_identity_review.py"
        path.write_text(
            path.read_text().replace("671_assignment_generation", PUBLISHED)
        )
        self.assert_refused("heads")

    def test_missing_revision_is_refused(self) -> None:
        (
            self.root / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        ).unlink()
        self.assert_refused("heads")

    def test_future_linear_extension_cannot_reuse_exception(self) -> None:
        self.write(migration("688_future", (NEW_BRIDGE,)))
        self.assert_refused("exact declared revision inventory")

    def test_extra_fork_is_refused(self) -> None:
        self.write(migration("extra", ("670_user_credential_version",)))
        self.assert_refused("heads")

    def test_duplicate_revision_is_refused_before_indexing(self) -> None:
        (self.root / "alembic/versions/duplicate.py").write_bytes(self.base[-1].source)
        self.assert_refused("Duplicate revision ID")

    def test_duplicate_base_revision_is_refused(self) -> None:
        self.base = (*self.base, self.base[-1])
        self.assert_refused("Duplicate revision ID")

    def test_missing_parent_is_refused(self) -> None:
        self.write(migration("orphan", ("missing",)))
        self.assert_refused("missing parent")

    def test_cycle_is_refused(self) -> None:
        self.write(migration("cycle_a", ("cycle_b",)))
        self.write(migration("cycle_b", ("cycle_a",)))
        self.assert_refused("cycle")

    def test_base_body_is_immutable(self) -> None:
        path = self.root / self.base[-1].path
        path.write_bytes(path.read_bytes() + b"\n# mutation\n")
        self.assert_refused("removed or altered")

    def test_removed_base_revision_is_refused(self) -> None:
        (self.root / self.base[-1].path).unlink()
        self.assert_refused("removed or altered")

    def test_multiple_current_host_heads_are_refused(self) -> None:
        self.base = (
            *self.base,
            migration("base_fork", ("670_user_credential_version",)),
        )
        self.write(self.base[-1])
        self.assert_refused("not single-headed")

    def test_executable_bridge_is_refused_by_owner(self) -> None:
        path = (
            self.root / "alembic/versions/683_field_lifecycle_connection_types_merge.py"
        )
        path.write_bytes(path.read_bytes() + b"\nprint('effect')\n")
        self.assert_refused("bridge must be inert")

    def test_bridge_parent_order_is_not_graph_authority(self) -> None:
        records = gate._inventory_in_tree(self.root)
        reordered = tuple(
            replace(item, parents=tuple(reversed(item.parents)))
            if item.revision in {BRIDGE, NEW_BRIDGE}
            else item
            for item in records
        )
        patch.object(gate, "_inventory_in_tree", return_value=reordered).start()
        self.assertEqual([], self.findings())

    def test_bridge_body_validator_refuses_executable_syntax(self) -> None:
        source = (
            REPOSITORY
            / "alembic/versions/683_field_lifecycle_connection_types_merge.py"
        ).read_text()
        mutations = (
            source + "\nprint('side effect')\n",
            source + "\nimport os\n",
            source.replace(
                "branch_labels: str | Sequence[str] | None = None",
                "branch_labels = 'branch'",
            ),
            source.replace(
                "depends_on: str | Sequence[str] | None = None",
                "depends_on = 'dependency'",
            ),
            source.replace("def upgrade()", "@run()\ndef upgrade()"),
            source.replace("def upgrade()", "def upgrade(value=run())"),
            source.replace("def upgrade() -> None:", "def upgrade() -> run():"),
            source.replace(
                "def upgrade() -> None:", "def upgrade() -> None:\n    run()"
            ),
            source.replace("revision: str =", "revision: run() ="),
        )
        self.assertTrue(gate._inert_bridge(source.encode(), DECLARATION.bridges[0]))
        for mutation in mutations:
            with self.subTest(mutation=mutation[-100:]):
                self.assertFalse(
                    gate._inert_bridge(mutation.encode(), DECLARATION.bridges[0])
                )

    def test_original_approval_alone_cannot_accept_router_amendment(self) -> None:
        self.approval.write_text("Status: accepted\n")
        self.assert_refused("amendment acceptance evidence is missing")

    def test_proposed_router_amendment_is_refused(self) -> None:
        self.approval.write_text(
            ACCEPTED_APPROVAL.replace(
                "Amendment status: accepted", "Amendment status: proposed"
            )
        )
        self.assert_refused("must be separately accepted")

    def test_missing_router_amendment_status_is_refused(self) -> None:
        self.approval.write_text(
            ACCEPTED_APPROVAL.replace("Amendment status: accepted", "")
        )
        self.assert_refused("must be separately accepted")

    def test_ambiguous_router_amendment_status_is_refused(self) -> None:
        self.approval.write_text(ACCEPTED_APPROVAL + "Amendment status: accepted\n")
        self.assert_refused("must be separately accepted")

    def test_duplicate_amendment_anchor_is_refused(self) -> None:
        self.approval.write_text(
            ACCEPTED_APPROVAL
            + DECLARATION.amendment_heading
            + "\nAmendment status: accepted\n"
        )
        self.assert_refused("acceptance evidence is missing or ambiguous")

    def test_unrelated_accepted_section_cannot_supply_amendment_status(self) -> None:
        self.approval.write_text(
            ACCEPTED_APPROVAL.replace(
                "Amendment status: accepted",
                "## Unrelated amendment\nAmendment status: accepted",
            )
        )
        self.assert_refused("must be separately accepted")

    def test_missing_published_router_is_refused(self) -> None:
        (self.root / "alembic/versions/672_router_health_reconciler.py").unlink()
        self.assert_refused("removed or altered")

    def test_altered_published_router_is_refused(self) -> None:
        path = self.root / "alembic/versions/672_router_health_reconciler.py"
        path.write_bytes(path.read_bytes() + b"\n# changed published body\n")
        self.assert_refused("removed or altered")

    def test_altered_new_bridge_hash_is_refused(self) -> None:
        path = self.root / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        path.write_bytes(path.read_bytes() + b"\n# changed bridge body\n")
        self.assert_refused("inventory/hash/parents mismatch")

    def test_new_bridge_executable_body_is_refused(self) -> None:
        path = self.root / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        path.write_text(
            path.read_text().replace(
                "def upgrade() -> None:", "def upgrade() -> None:\n    run()"
            )
        )
        self.assert_refused("bridge must be inert")

    def test_new_bridge_wrong_parent_is_refused(self) -> None:
        path = self.root / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        path.write_text(path.read_text().replace(PUBLISHED_ROUTER, PUBLISHED))
        self.assert_refused("heads")

    def test_extra_converged_branch_is_refused(self) -> None:
        self.write(migration("extra", (PUBLISHED_ROUTER,)))
        path = self.root / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        path.write_text(
            path.read_text().replace(
                '    "672_router_health_reconciler",',
                '    "672_router_health_reconciler",\n    "extra",',
            )
        )
        self.assert_refused("exact declared revision inventory")

    def test_original_bridge_cannot_be_reparented_to_router(self) -> None:
        path = (
            self.root / "alembic/versions/683_field_lifecycle_connection_types_merge.py"
        )
        path.write_text(path.read_text().replace(PUBLISHED, PUBLISHED_ROUTER))
        self.assert_refused("inventory/hash/parents mismatch")

    def test_original_defaults_cannot_be_reparented(self) -> None:
        path = self.root / "alembic/versions/684_customer_connection_type_defaults.py"
        path.write_text(path.read_text().replace(BRIDGE, PUBLISHED_ROUTER))
        self.assert_refused("heads")

    def test_field_identity_old_filename_is_refused(self) -> None:
        path = self.root / "alembic/versions/687_field_identity_review.py"
        path.rename(path.with_name("672_field_identity_review.py"))
        self.assert_refused("inventory/hash/parents mismatch")

    def test_field_identity_renamed_body_stays_immutable(self) -> None:
        path = self.root / "alembic/versions/687_field_identity_review.py"
        path.write_bytes(path.read_bytes() + b"\n# changed renamed body\n")
        self.assert_refused("inventory/hash/parents mismatch")

    def test_altered_published_pair_declaration_is_refused(self) -> None:
        patch.object(
            gate,
            "FIELD_LIFECYCLE_PRESERVATION",
            replace(DECLARATION, published_revisions=(PUBLISHED, BRIDGE)),
        ).start()
        self.assert_refused("Missing or altered")

    def test_altered_bridge_contract_is_refused(self) -> None:
        bridge = replace(
            DECLARATION.bridges[-1],
            parents=("684_customer_connection_type_defaults", PUBLISHED),
        )
        patch.object(
            gate,
            "FIELD_LIFECYCLE_PRESERVATION",
            replace(DECLARATION, bridges=(*DECLARATION.bridges[:-1], bridge)),
        ).start()
        self.assert_refused("Missing or altered")

    def test_new_bridge_inert_validator_checks_identity_and_metadata(self) -> None:
        path = (
            REPOSITORY / "alembic/versions/686_field_lifecycle_router_health_merge.py"
        )
        source = path.read_text()
        self.assertTrue(gate._inert_bridge(source.encode(), DECLARATION.bridges[-1]))
        for changed in (
            source.replace(NEW_BRIDGE, "other_merge"),
            source.replace(PUBLISHED_ROUTER, PUBLISHED),
            source.replace("branch_labels: None = None", "branch_labels = 'branch'"),
            source.replace("depends_on: None = None", "depends_on = 'dependency'"),
            source + "\nrun()\n",
            source.replace("def upgrade()", "@run()\ndef upgrade()"),
        ):
            with self.subTest(changed=changed[-100:]):
                self.assertFalse(
                    gate._inert_bridge(changed.encode(), DECLARATION.bridges[-1])
                )


class OrdinaryLinearAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / gate.VERSIONS_DIR).mkdir(parents=True)
        self.base = (migration("base", ()),)
        self.current = self.base
        self.addCleanup(patch.stopall)
        patch.object(
            gate,
            "_git",
            side_effect=lambda *args: "old" if args[0] == "merge-base" else "current",
        ).start()
        patch.object(
            gate,
            "_inventory_at",
            side_effect=lambda ref: self.base if ref == "old" else self.current,
        ).start()
        self.write(self.base[0])

    def write(self, item: gate.MigrationSource) -> None:
        (self.root / item.path).write_bytes(item.source)

    def findings(self) -> list[str]:
        return gate.check("main", self.root)

    def test_ordinary_linear_extension_stays_admissible(self) -> None:
        self.write(migration("child", ("base",)))
        self.write(migration("grandchild", ("child",)))
        self.assertEqual([], self.findings())

    def test_no_migration_change_stays_admissible(self) -> None:
        self.assertEqual([], self.findings())

    def test_stale_fork_is_refused(self) -> None:
        self.current = (*self.base, migration("current_head", ("base",)))
        self.write(migration("stale_child", ("base",)))
        self.assertIn("heads", "\n".join(self.findings()))

    def test_arbitrary_merge_cannot_bypass_linear_rule(self) -> None:
        self.write(migration("left", ("base",)))
        self.write(migration("right", ("base",)))
        self.write(migration("merge", ("left", "right")))
        self.assertIn("linear segment", "\n".join(self.findings()))

    def test_missing_revision_metadata_is_refused(self) -> None:
        (self.root / "alembic/versions/invalid.py").write_text(
            "down_revision = 'base'\n"
        )
        self.assertIn("must declare", "\n".join(self.findings()))

    def test_dynamic_metadata_is_refused(self) -> None:
        path = self.root / "alembic/versions/invalid.py"
        path.write_text("revision = compute()\ndown_revision = 'base'\n")
        self.assertIn("literal and parseable", "\n".join(self.findings()))

    def test_repeated_metadata_is_refused(self) -> None:
        path = self.root / "alembic/versions/invalid.py"
        path.write_text(
            "revision = 'child'\nrevision = 'hidden'\ndown_revision = 'base'\n"
        )
        self.assertIn("Repeated revision", "\n".join(self.findings()))

    def test_duplicate_parents_are_refused(self) -> None:
        path = self.root / "alembic/versions/invalid.py"
        path.write_text("revision = 'child'\ndown_revision = ('base', 'base')\n")
        self.assertIn("Invalid down_revision", "\n".join(self.findings()))

    def test_unchanged_inventory_cannot_hide_base_mutation(self) -> None:
        path = self.root / self.base[0].path
        path.write_bytes(path.read_bytes() + b"# changed\n")
        self.assertIn("removed or altered", "\n".join(self.findings()))


if __name__ == "__main__":
    unittest.main()
