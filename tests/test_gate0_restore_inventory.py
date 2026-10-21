"""Offline inventory decoding/parity tests; no database acceptance claimed."""

import io
import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from scripts import restore_drill as drill
from scripts import restore_inventory as inventory


def source_document() -> str:
    roles = ("app_user", "platform_api")
    tables = ("outbox_events", "platform_outbox_events")
    return json.dumps(
        {
            "schema": inventory.SCHEMA,
            "roles": [[r, True, False, False] for r in roles],
            "schemas": [["public", "pg_database_owner"]],
            "tables": [["public", t, "platform_api", True, True] for t in tables],
            "policies": [["public", "outbox_events", "tenant_isolation"]],
            "heads": ["557"],
            "privileges": [
                [r, "public", None, p, p == "USAGE"]
                for r in roles
                for p in ("USAGE", "CREATE")
            ]
            + [
                [r, "public", t, p, not (r == "app_user" and t.startswith("platform"))]
                for r in roles
                for t in tables
                for p in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                )
            ],
        }
    )


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = source_document()
        self.source = inventory.parse_inventory(self.raw)

    def test_allowed_and_denied_observations_cover_both_planes(self) -> None:
        self.assertEqual(len(self.source.privileges), 32)
        self.assertTrue(
            any(
                p.allowed and p.table == "outbox_events" and p.role == "platform_api"
                for p in self.source.privileges
            )
        )
        self.assertTrue(
            any(
                not p.allowed
                and p.table == "platform_outbox_events"
                and p.role == "app_user"
                for p in self.source.privileges
            )
        )
        self.assertIsNone(inventory.first_divergence(self.source, self.source))

    def test_each_metadata_category_detects_loss(self) -> None:
        mutations = (
            ("roles", replace(self.source, roles=())),
            ("schemas", replace(self.source, schemas=())),
            ("tables", replace(self.source, tables=())),
            ("policies", replace(self.source, policies=())),
            ("heads", replace(self.source, heads=("old",))),
            ("privileges", replace(self.source, privileges=())),
        )
        for category, changed in mutations:
            with self.subTest(category=category):
                self.assertEqual(
                    inventory.first_divergence(self.source, changed), category
                )

    def test_privilege_leak_and_role_posture_drift_are_detected(self) -> None:
        changed = replace(
            self.source,
            roles=(replace(self.source.roles[0], superuser=True),)
            + self.source.roles[1:],
        )
        self.assertEqual(inventory.first_divergence(self.source, changed), "roles")
        changed = replace(
            self.source,
            privileges=(
                replace(
                    self.source.privileges[0],
                    allowed=not self.source.privileges[0].allowed,
                ),
            )
            + self.source.privileges[1:],
        )
        self.assertEqual(inventory.first_divergence(self.source, changed), "privileges")

    def test_strict_shape_types_duplicates_and_empty_scope(self) -> None:
        invalid = (
            "not JSON",
            "[]",
            self.raw.replace(inventory.SCHEMA, "future"),
            self.raw.replace('"heads": ["557"]', '"heads": []'),
            self.raw.replace('"heads": ["557"]', '"heads": ["557", "557"]'),
            self.raw.replace('"roles":', '"unexpected":'),
            self.raw.replace(
                '["app_user", true, false, false]', '["app_user", 1, false, false]'
            ),
            self.raw.replace('"schema":', '"schema": "duplicate", "schema":'),
            self.raw.replace('"USAGE"', '"EXECUTE"'),
            self.raw.replace(
                '"platform_api", true, false, false', '"app_user", true, false, false'
            ),
        )
        for raw in invalid:
            with self.subTest(raw=raw[:25]), self.assertRaises(ValueError):
                inventory.parse_inventory(raw)

    def test_matrix_omission_duplicate_and_contradiction_fail(self) -> None:
        entry = '["app_user", "public", null, "USAGE", true]'
        for raw in (
            self.raw.replace(entry + ", ", "", 1),
            self.raw.replace(entry, entry + ", " + entry, 1),
            self.raw.replace(entry, entry + ", " + entry.replace("true", "false"), 1),
        ):
            with self.subTest(raw=raw[-40:]), self.assertRaises(ValueError):
                inventory.parse_inventory(raw)

    def test_comparator_diagnostics_exclude_values(self) -> None:
        secret = "private SQL payload"
        changed = replace(self.source, heads=(secret,))
        self.assertEqual(inventory.first_divergence(self.source, changed), "heads")

    def test_collection_uses_shared_query(self) -> None:
        raw = self.raw

        class Reader:
            def scalar(self, sql_text: str) -> str:
                if sql_text != inventory.INVENTORY_SQL:
                    raise AssertionError("query diverged")
                return raw

        self.assertEqual(inventory.collect_inventory(Reader()), self.source)
        self.assertIn("has_schema_privilege", inventory.INVENTORY_SQL)
        self.assertIn("has_table_privilege", inventory.INVENTORY_SQL)
        self.assertNotIn("information_schema.table_privileges", inventory.INVENTORY_SQL)

    def test_restore_owner_compares_before_cleanup_and_sanitizes_failure(self) -> None:
        for observed in (
            self.raw,
            self.raw.replace('"557"', '"old"'),
            RuntimeError("private SQL payload"),
        ):
            with self.subTest(observed=type(observed)):
                events: list[str] = []
                pg = MagicMock()
                pg.__enter__.return_value = pg
                pg.__exit__.side_effect = lambda *exc, log=events: log.append("cleanup")
                pg.psql.return_value = subprocess.CompletedProcess([], 0, b"", b"")

                def scalar(
                    sql_text: str,
                    observation: str | RuntimeError = observed,
                    log: list[str] = events,
                ) -> str:
                    self.assertEqual(sql_text, inventory.INVENTORY_SQL)
                    log.append("inventory")
                    if isinstance(observation, RuntimeError):
                        raise observation
                    return observation

                pg.scalar.side_effect = scalar
                with (
                    patch.multiple(
                        drill,
                        ScratchPostgres=Mock(return_value=pg),
                        load_bundle=Mock(
                            return_value=(
                                {},
                                {
                                    "globals": Path("globals"),
                                    "database_dump": Path("dump"),
                                },
                            )
                        ),
                        verify_hashes=Mock(return_value=[]),
                        parse_globals_roles=Mock(
                            return_value=({"app_user", "platform_api"}, False)
                        ),
                        assert_isolation_model=Mock(return_value=[]),
                    ),
                    patch.object(
                        drill.gzip, "open", side_effect=lambda *args: io.BytesIO(b"")
                    ),
                ):
                    result = drill.drill_bundle(
                        Path("manifest"), "unused", expected_inventory=self.source
                    )
                self.assertEqual(events, ["inventory", "cleanup"])
                self.assertEqual(
                    result["verdict"], "PASS" if observed == self.raw else "FAIL"
                )
                self.assertNotIn("private SQL payload", json.dumps(result))
