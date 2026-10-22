"""Offline input and execution-boundary tests; no Docker process is executed."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import restore_drill as drill
from scripts.restore_guard import HostPermit, RestoreRefusal


class RestoreBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.manifest = self.base / "bundle.manifest.json"
        self.doc = {
            "schema": "dotmac_sub.recovery_bundle.v1",
            "database": "dotmac_sub",
            "created_at": "2026-10-22T00:00:00Z",
            "git_sha": "a" * 40,
            "artifacts": [],
        }
        for role in ("globals", "database_dump"):
            path = self.base / (role + ".gz")
            path.write_bytes(role.encode())
            self.doc["artifacts"].append(
                {
                    "role": role,
                    "file": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )

    def write(self) -> None:
        self.manifest.write_text(json.dumps(self.doc))

    def test_typed_bundle_hashes_and_duplicate_fields(self) -> None:
        self.write()
        bundle, artifacts = drill.load_bundle(self.manifest)
        self.assertIsInstance(bundle, drill.RecoveryBundle)
        self.assertEqual(drill.verify_hashes(bundle, artifacts), [])
        self.manifest.write_text(
            self.manifest.read_text().replace(
                '"database": "dotmac_sub"',
                '"database": "dotmac_sub", "database": "other"',
            )
        )
        with self.assertRaisesRegex(ValueError, "duplicate_bundle_field"):
            drill.load_bundle(self.manifest)

    def test_artifact_traversal_absolute_and_symlink_refuse(self) -> None:
        for name in ("../outside.gz", "/outside.gz", "sub/file.gz"):
            with self.subTest(name=name):
                self.doc["artifacts"][0]["file"] = name
                self.write()
                with self.assertRaisesRegex(ValueError, "bundle_path"):
                    drill.load_bundle(self.manifest)
        self.doc["artifacts"][0]["file"] = "link.gz"
        (self.base / "link.gz").symlink_to(self.base / "globals.gz")
        self.write()
        with self.assertRaisesRegex(ValueError, "invalid_bundle_artifact"):
            drill.load_bundle(self.manifest)

    def test_duplicate_artifact_and_untyped_size_refuse(self) -> None:
        first = self.doc["artifacts"][0]
        self.doc["artifacts"][1] = first.copy()
        self.write()
        with self.assertRaisesRegex(ValueError, "duplicate_bundle_artifact"):
            drill.load_bundle(self.manifest)
        first["bytes"] = True
        self.write()
        with self.assertRaisesRegex(ValueError, "invalid_bundle_artifact"):
            drill.load_bundle(self.manifest)

    def test_hash_failure_is_a_fixed_public_code(self) -> None:
        self.doc["artifacts"][0]["sha256"] = "0" * 64
        self.write()
        bundle, artifacts = drill.load_bundle(self.manifest)
        self.assertEqual(drill.verify_hashes(bundle, artifacts), ["artifact_hash"])

    def test_host_refusal_precedes_operational_process(self) -> None:
        with (
            patch.object(
                drill, "enrolled", side_effect=RestoreRefusal("host_identity")
            ),
            patch.object(drill.subprocess, "run") as process,
        ):
            with self.assertRaisesRegex(RestoreRefusal, "^host_identity$"):
                drill.run(["docker", "info"])
            process.assert_not_called()

    def test_permit_duplicate_fields_refuse(self) -> None:
        self.manifest.write_text(
            '{"purpose":"restore","hostname":"vmi3537655","hostname":"hp-server","machine_sha256":"'
            + "a" * 64
            + '","docker_sha256":"'
            + "b" * 64
            + '"}'
        )
        with patch("scripts.restore_guard.protected"):
            with self.assertRaisesRegex(RestoreRefusal, "host_permit_invalid"):
                HostPermit.load(self.manifest)

    def test_permit_rejects_nonobjects_wrong_types_and_oversize(self) -> None:
        documents = [
            [],
            {
                "purpose": "restore",
                "hostname": 7,
                "machine_sha256": "a" * 64,
                "docker_sha256": "b" * 64,
            },
            "x" * 4097,
        ]
        with patch("scripts.restore_guard.protected"):
            for document in documents:
                with self.subTest(document=type(document).__name__):
                    self.manifest.write_text(json.dumps(document))
                    with self.assertRaisesRegex(RestoreRefusal, "host_permit_invalid"):
                        HostPermit.load(self.manifest)

    def test_docker_uses_fixed_binary_socket_and_scrubbed_environment(self) -> None:
        with (
            patch.object(drill, "enrolled"),
            patch.object(drill.subprocess, "run") as process,
        ):
            drill.run(["docker", "info"], timeout=12)
        self.assertEqual(
            process.call_args.args[0],
            ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "info"],
        )
        self.assertEqual(
            process.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C"}
        )
        self.assertEqual(process.call_args.kwargs["timeout"], 12)

    def test_malformed_artifact_role_refuses_without_untyped_exception(self) -> None:
        self.doc["artifacts"][0]["role"] = ["globals"]
        self.write()
        with self.assertRaisesRegex(ValueError, "invalid_bundle_artifact"):
            drill.load_bundle(self.manifest)

    def test_real_postgres_boolean_text_drives_role_posture_checks(self) -> None:
        from unittest.mock import MagicMock

        contract = {
            "app_admin": (True, True, False),
            "app_user": (True, False, False),
            "dotmac_public_probe": (False, False, False),
        }
        canonical = "app_admin|true|true|false\napp_user|true|false|false\ndotmac_public_probe|false|false|false"
        for rows, valid in (
            (canonical, True),
            (canonical.replace("app_admin|true|true", "app_admin|false|true"), False),
        ):
            with self.subTest(valid=valid):
                pg = MagicMock()
                pg.scalar.side_effect = [rows, "1|0", "0", "f", "f", "2|0|2"]
                with patch.object(
                    drill, "expected_role_contract", return_value=(contract, ())
                ):
                    checks = drill.assert_isolation_model(
                        pg, globals_roles=set(contract)
                    )
                posture = next(c for c in checks if c.name == "contract_role_postures")
                self.assertEqual(posture.passed, valid)
                self.assertTrue(
                    next(c for c in checks if c.name == "globals_roles_restored").passed
                )

    def test_cli_out_failure_never_prints_private_path(self) -> None:
        import io

        with (
            patch.object(
                drill.sys,
                "argv",
                [
                    "restore",
                    "--manifest",
                    "fixture",
                    "--dry-run",
                    "--out",
                    str(self.base / "SECRET_SENTINEL" / "output"),
                ],
            ),
            patch.object(drill, "dry_run", return_value={"verdict": "DRY_RUN_OK"}),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(drill.main(), drill.EXIT_INFRA)
        self.assertEqual(
            json.loads(output.getvalue())["reason"], "restore_output_failed"
        )
        self.assertNotIn("SECRET_SENTINEL", output.getvalue())

    def test_corrupt_deflate_is_sanitized_by_actual_dry_run(self) -> None:
        import gzip
        import io

        for artifact in self.doc["artifacts"]:
            path = self.base / artifact["file"]
            data = bytearray(gzip.compress(b"CREATE ROLE app_user;\n"))
            if artifact["role"] == "globals":
                data[10] = (
                    7  # Reserved DEFLATE block type; zlib.error, not BadGzipFile.
                )
            path.write_bytes(data)
            artifact["bytes"] = len(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
        self.write()
        with (
            patch.object(
                drill.sys,
                "argv",
                ["restore", "--manifest", str(self.manifest), "--dry-run"],
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertNotEqual(drill.main(), 0)
        self.assertEqual(
            json.loads(output.getvalue())["reason"], "restore_validation_failed"
        )
        self.assertNotIn(str(self.base), output.getvalue())
