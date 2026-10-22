"""Control-session faults and private capture admission; no Docker is executed."""

import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

from deploy.gate0 import proof5_restore as proof
from deploy.gate0 import restore_capture as capture
from scripts.restore_guard import RestoreRefusal
from scripts.restore_inventory import Inventory


def time_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def expected_plan(capture_sha256: str | None = None) -> capture.CapturePlan:
    return capture.CapturePlan(
        context={
            "run_id": "00000000-0000-4000-8000-000000000001",
            "phase": "initial",
            "prior_run_id": None,
            "source_revision": "c" * 40,
            "source_tree": "d" * 40,
            "deployment_ref": "dotmac_sub/staging/seabone",
            "tool_revision": "e" * 40,
            "host_key_id": "host-key",
            "action_refs": ["authorized-capture"],
            "deadline": time_text(datetime.now(UTC) + timedelta(hours=1)),
        },
        observation_not_before=time_text(datetime.now(UTC) - timedelta(hours=12)),
        source_identity=capture.SOURCE,
        test_host_identity="vmi3537655",
        owner_sha256="sha256:" + "a" * 64,
        library_sha256="sha256:" + "f" * 64,
        source_permit_sha256="sha256:" + "b" * 64,
        restore_permit_sha256="sha256:" + "9" * 64,
        capture_tool_sha256="sha256:" + "1" * 64,
        restore_tool_sha256="sha256:" + "2" * 64,
        image="postgres@sha256:" + "a" * 64,
        capture_sha256=capture_sha256,
    )


def write_private_capture(
    directory: Path,
) -> tuple[capture.CapturePlan, Inventory, str]:
    from dataclasses import asdict

    from scripts.restore_drill import inventory_digest, sha256_file
    from scripts.restore_inventory import parse_inventory
    from tests.test_gate0_restore_inventory import source_document

    raw = source_document()
    inventory = parse_inventory(raw)
    (directory / "inventory.json").write_text(raw)
    artifacts = []
    for role in ("globals", "database_dump"):
        path = directory / (role + ".gz")
        path.write_bytes(b"fixture")
        artifacts.append(
            {
                "role": role,
                "file": path.name,
                "bytes": 7,
                "sha256": sha256_file(path),
            }
        )
    plan = expected_plan()
    started = time_text(datetime.now(UTC) - timedelta(minutes=2))
    finished = time_text(datetime.now(UTC) - timedelta(minutes=1))
    manifest = directory / "fixture.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "dotmac_sub.recovery_bundle.v1",
                "database": "dotmac_sub",
                "created_at": started,
                "git_sha": "c" * 40,
                "artifacts": artifacts,
            }
        )
    )
    record = capture.RestoreCapture(
        capture.SOURCE,
        started,
        finished,
        sha256_file(manifest),
        inventory_digest(inventory),
        sha256_file(directory / "inventory.json"),
        "a" * 64,
        "b" * 64,
        capture.FreezeObservation(5, tuple(range(1, 25))),
        plan.context,
        "c" * 40,
        "d" * 40,
        "e" * 40,
        "f" * 64,
        "1" * 64,
        "2" * 64,
    )
    (directory / "capture.json").write_text(json.dumps(asdict(record)))
    plan = replace(
        plan, capture_sha256="sha256:" + sha256_file(directory / "capture.json")
    )
    return plan, inventory, raw


class CaptureControlTests(TestCase):
    def test_lock_observation_requires_full_exact_granted_set(self) -> None:
        value = {"pid": 23, "required": list(range(1, 25)), "held": list(range(1, 25))}
        observed = capture.FreezeObservation.parse(json.dumps(value))
        self.assertEqual(observed.pid, 23)
        for mutation in (
            {"held": list(range(1, 24))},
            {"held": list(range(1, 26))},
            {"pid": True},
            {"required": [1, 1]},
            {"held": None},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(RestoreRefusal, "capture_lock_observation"):
                    capture.FreezeObservation.parse(json.dumps(value | mutation))

    def test_duplicate_observation_refused(self) -> None:
        with self.assertRaisesRegex(RestoreRefusal, "capture_lock_observation"):
            capture.FreezeObservation.parse('{"pid":1,"pid":2,"held":[],"required":[]}')

    def test_capture_enrollment_precedes_every_process(self) -> None:
        with (
            patch.object(
                capture, "enrolled", side_effect=RestoreRefusal("host_identity")
            ),
            patch.object(capture.subprocess, "Popen") as process,
        ):
            with self.assertRaisesRegex(RestoreRefusal, "host_identity"):
                with capture.CatalogFreeze():
                    self.fail("entered refused host")
            process.assert_not_called()

    def test_sql_lock_failure_rolls_back_and_never_enters_capture(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        process.communicate.return_value = ("", "")
        with (
            patch.object(capture, "enrolled"),
            patch.object(capture.subprocess, "Popen", return_value=process),
            patch.object(
                capture.CatalogFreeze,
                "observe",
                side_effect=RestoreRefusal("capture_freeze_lost"),
            ),
        ):
            with self.assertRaisesRegex(RestoreRefusal, "capture_freeze_lost"):
                with capture.CatalogFreeze():
                    self.fail("entered failed transaction")
        self.assertTrue(
            any("ROLLBACK" in c.args[0] for c in process.stdin.write.call_args_list)
        )

    def test_holder_timeout_kills_and_waits(self) -> None:
        holder = capture.CatalogFreeze()
        holder.process = MagicMock()
        holder.process.poll.return_value = None
        holder.process.communicate.side_effect = [
            subprocess.TimeoutExpired("psql", 10),
            ("", ""),
        ]
        holder.close()
        holder.process.kill.assert_called_once()
        self.assertEqual(holder.process.communicate.call_count, 2)

    def test_shared_catalog_and_migration_controls_are_required(self) -> None:
        self.assertIn("n.nspname='pg_catalog'", capture.FREEZE_SQL)
        self.assertIn(
            "LOCK TABLE public.alembic_version IN SHARE MODE", capture.FREEZE_SQL
        )
        self.assertIn("mode='ShareLock' AND granted", capture.OBSERVE_SQL)
        self.assertIn("pg_backend_pid()", capture.OBSERVE_SQL)

    def test_proof_host_refusal_precedes_file_and_docker_access(self) -> None:
        with (
            patch.object(
                proof, "enrolled", side_effect=RestoreRefusal("host_identity")
            ),
            patch.object(proof, "protected_directory") as directory,
            patch.object(proof, "drill_bundle") as drill,
        ):
            with self.assertRaisesRegex(RestoreRefusal, "host_identity"):
                proof.proof5(
                    Path("/not/read"), "postgres@sha256:" + "a" * 64, Path("plan")
                )
            directory.assert_not_called()
            drill.assert_not_called()

    def test_unpinned_image_refused_before_private_payload(self) -> None:
        with (
            patch.object(proof, "enrolled"),
            patch.object(proof, "protected_directory") as directory,
        ):
            with self.assertRaisesRegex(RestoreRefusal, "restore_image_unpinned"):
                proof.proof5(Path("/not/read"), "postgres:16-alpine", Path("plan"))
            directory.assert_not_called()

    def test_lost_session_refuses_subsequent_queries(self) -> None:
        holder = capture.CatalogFreeze()
        holder.process = MagicMock()
        holder.process.poll.return_value = 1
        with self.assertRaisesRegex(RestoreRefusal, "capture_freeze_lost"):
            holder.scalar("SELECT 1;")
        holder.process.stdin.write.assert_not_called()

    def pipe_holder(self) -> tuple[capture.CatalogFreeze, BinaryIO]:
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r", encoding="utf-8")
        writer = os.fdopen(write_fd, "wb", buffering=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        holder = capture.CatalogFreeze()
        holder.process = MagicMock()
        holder.process.poll.return_value = None
        holder.process.stdout = reader
        return holder, writer

    def test_observation_timeout_refuses(self) -> None:
        holder, _ = self.pipe_holder()
        with patch.object(capture, "READ_TIMEOUT_SECONDS", 0.02):
            with self.assertRaisesRegex(RestoreRefusal, "capture_query_timeout"):
                holder.observe()

    def test_partial_pipe_record_cannot_block_past_deadline(self) -> None:
        holder, writer = self.pipe_holder()
        writer.write(b'{"pid": 42')
        started = time.monotonic()
        with patch.object(capture, "READ_TIMEOUT_SECONDS", 0.02):
            with self.assertRaisesRegex(RestoreRefusal, "capture_query_timeout"):
                holder.observe()
        self.assertLess(time.monotonic() - started, 1)

    def test_oversized_and_partial_eof_records_refuse(self) -> None:
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                holder, writer = self.pipe_holder()
                writer.write(b"0123456789abcdef")
                if not oversized:
                    writer.close()
                with patch.object(capture, "MAX_RECORD_BYTES", 8 if oversized else 64):
                    with self.assertRaisesRegex(
                        RestoreRefusal,
                        "capture_record_oversized"
                        if oversized
                        else "capture_freeze_lost",
                    ):
                        holder.scalar("SELECT 1;")

    def test_reader_preserves_complete_following_record(self) -> None:
        holder, writer = self.pipe_holder()
        writer.write(b"first\nsecond\n")
        self.assertEqual(holder.scalar("SELECT 1;"), "first")
        # No further pipe activity; the second query consumes the buffered line.
        self.assertEqual(holder.scalar("SELECT 2;"), "second")

    def test_backup_failure_releases_control_and_never_writes_capture(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            holder = MagicMock()
            holder.__enter__.return_value = holder
            with (
                patch.object(capture, "enrolled"),
                patch.object(capture, "protected"),
                patch.object(capture, "protected_directory"),
                patch.object(capture, "CatalogFreeze", return_value=holder),
                patch.object(capture.CapturePlan, "load", return_value=expected_plan()),
                patch.object(capture, "pinned_file"),
                patch.object(capture, "verify_tools", return_value="e" * 40),
                patch.object(
                    capture, "local_git_identity", return_value=("c" * 40, "d" * 40)
                ),
                patch.object(
                    capture.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1),
                ),
            ):
                with self.assertRaisesRegex(RestoreRefusal, "capture_owner_failed"):
                    capture.capture(directory, directory, Path("plan"))
            holder.__exit__.assert_called_once()
            self.assertEqual(list(directory.rglob("capture.json")), [])

    def test_proof_requires_actual_parity_cleanup_and_equal_inventory_digest(
        self,
    ) -> None:
        import tempfile

        from scripts.restore_drill import (
            RestoreOutcome,
            RestoreVerdict,
            inventory_digest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan, inventory, raw = write_private_capture(directory)
            for parity, absent, digest, passed in (
                (True, True, inventory_digest(inventory), True),
                (False, True, inventory_digest(inventory), False),
                (True, False, inventory_digest(inventory), False),
                (True, True, "a" * 64, False),
            ):
                with self.subTest(parity=parity, absent=absent, digest=digest):
                    result = RestoreOutcome(
                        RestoreVerdict.PASS,
                        "restore_and_assert",
                        (),
                        parity,
                        absent,
                        digest,
                    )
                    with (
                        patch.object(
                            proof,
                            "enrolled",
                            return_value=MagicMock(hostname="vmi3537655"),
                        ),
                        patch.object(proof, "protected_directory"),
                        patch.object(proof.CapturePlan, "load", return_value=plan),
                        patch.object(proof, "pinned_file"),
                        patch.object(proof, "verify_tools"),
                        patch.object(proof, "protected"),
                        patch.object(proof, "drill_bundle", return_value=result),
                    ):
                        outcome = proof.proof5(
                            directory, "postgres@sha256:" + "a" * 64, Path("plan")
                        )
                    self.assertEqual(outcome["outcome"] == "pass", passed)
            # Corrupt private binding refuses before restore owner invocation.
            (directory / "inventory.json").write_text(raw + " ")
            with (
                patch.object(proof, "enrolled"),
                patch.object(proof, "protected_directory"),
                patch.object(proof.CapturePlan, "load", return_value=plan),
                patch.object(proof, "pinned_file"),
                patch.object(proof, "verify_tools"),
                patch.object(proof, "protected"),
                patch.object(proof, "drill_bundle") as drill,
            ):
                with self.assertRaisesRegex(RestoreRefusal, "capture_invalid"):
                    proof.proof5(directory, "postgres@sha256:" + "a" * 64, Path("plan"))
                drill.assert_not_called()

    def test_capture_holder_absence_is_observed_after_disconnect(self) -> None:
        holder = capture.CatalogFreeze()
        holder.before = capture.FreezeObservation(23, tuple(range(1, 25)))
        holder.process = MagicMock()
        holder.process.poll.return_value = 0
        for observed in ("t\n", "f\n", ""):
            with (
                self.subTest(observed=observed),
                patch.object(capture, "enrolled"),
                patch.object(
                    capture.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, observed, ""),
                ) as query,
            ):
                if observed == "t\n":
                    holder.close()
                else:
                    with self.assertRaisesRegex(
                        RestoreRefusal, "capture_cleanup_unproved"
                    ):
                        holder.close()
                self.assertIn("capture_pid=23", query.call_args.args[0])

    def test_protected_provenance_rejects_mismatch_before_restore(self) -> None:
        import tempfile

        from scripts.restore_drill import sha256_file

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan, _, _ = write_private_capture(directory)
            record_path = directory / "capture.json"
            manifest_path = directory / "fixture.manifest.json"
            original_record = record_path.read_text()
            original_manifest = manifest_path.read_text()
            cases = (
                "unapproved_hash",
                "owner_sha256",
                "library_sha256",
                "permit_sha256",
                "capture_tool_sha256",
                "restore_tool_sha256",
                "source_revision",
                "source_tree",
                "tool_revision",
                "context",
                "database",
                "git_sha",
                "created_at",
                "future_interval",
            )
            for case in cases:
                with self.subTest(case=case):
                    record = json.loads(original_record)
                    manifest = json.loads(original_manifest)
                    if case == "unapproved_hash":
                        record["inventory_sha256"] = "0" * 64
                    elif case in {"database", "git_sha", "created_at"}:
                        manifest[case] = {
                            "database": "unrelated",
                            "git_sha": "not-a-revision",
                            "created_at": "2000-01-01T00:00:00Z",
                        }[case]
                    elif case == "future_interval":
                        record["started_at"] = time_text(
                            datetime.now(UTC) + timedelta(minutes=1)
                        )
                        record["finished_at"] = time_text(
                            datetime.now(UTC) + timedelta(minutes=2)
                        )
                    elif case == "context":
                        record["context"]["action_refs"] = ["unrelated-authorization"]
                    else:
                        record[case] = "0" * (
                            40
                            if case
                            in {"source_revision", "source_tree", "tool_revision"}
                            else 64
                        )
                    manifest_path.write_text(json.dumps(manifest))
                    record["bundle_sha256"] = sha256_file(manifest_path)
                    record_path.write_text(json.dumps(record))
                    approved = (
                        plan
                        if case == "unapproved_hash"
                        else replace(
                            plan, capture_sha256="sha256:" + sha256_file(record_path)
                        )
                    )
                    with (
                        patch.object(proof, "enrolled"),
                        patch.object(proof.CapturePlan, "load", return_value=approved),
                        patch.object(proof, "pinned_file"),
                        patch.object(proof, "verify_tools"),
                        patch.object(proof, "protected_directory"),
                        patch.object(proof, "protected"),
                        patch.object(proof, "drill_bundle") as owner,
                    ):
                        with self.assertRaises(RestoreRefusal):
                            proof.proof5(directory, plan.image, Path("plan"))
                        owner.assert_not_called()

    def test_expected_plan_is_strict_and_protected(self) -> None:
        import tempfile
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            original = asdict(expected_plan())
            path.write_text(json.dumps(original))
            with patch.object(
                capture,
                "protected",
                side_effect=RestoreRefusal("unprotected_execution_input"),
            ):
                with self.assertRaisesRegex(
                    RestoreRefusal, "unprotected_execution_input"
                ):
                    capture.CapturePlan.load(path)
            with patch.object(capture, "protected"):
                self.assertEqual(
                    capture.CapturePlan.load(path).context, original["context"]
                )
                for key, changed in (
                    ("owner_sha256", "arbitrary"),
                    ("library_sha256", True),
                    ("image", "postgres:16"),
                    ("source_identity", "foreign"),
                    ("context", {"run_id": "bad"}),
                    ("extra", "field"),
                ):
                    with self.subTest(key=key):
                        path.write_text(json.dumps(original | {key: changed}))
                        with self.assertRaisesRegex(
                            RestoreRefusal, "capture_plan_invalid"
                        ):
                            capture.CapturePlan.load(path)
                path.write_text(
                    json.dumps(original).replace(
                        '"image":', '"image":"duplicate", "image":'
                    )
                )
                with self.assertRaisesRegex(RestoreRefusal, "capture_plan_invalid"):
                    capture.CapturePlan.load(path)

    def test_pinned_inputs_and_dirty_git_are_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owner.sh"
            path.write_text("fixture")
            with patch.object(capture, "protected"):
                with self.assertRaisesRegex(RestoreRefusal, "capture_tool_unpinned"):
                    capture.pinned_file(path, "sha256:" + "0" * 64)
            for status in (" M scripts/db_backup.sh\n", "?? unreviewed.py\n"):
                results = [
                    subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
                    subprocess.CompletedProcess([], 0, "b" * 40 + "\n", ""),
                    subprocess.CompletedProcess([], 0, status, ""),
                ]
                with (
                    patch.object(capture, "protected"),
                    patch.object(capture.subprocess, "run", side_effect=results),
                ):
                    with self.assertRaisesRegex(
                        RestoreRefusal, "capture_checkout_unproved"
                    ):
                        capture.local_git_identity(Path(tmp))

    def test_proof_cli_sanitizes_corrupt_deflate_and_write_failures(self) -> None:
        import io
        import sys
        import zlib

        argv = [
            "proof5",
            "--capture",
            "/SECRET_SENTINEL/absent",
            "--image",
            "postgres@sha256:" + "a" * 64,
            "--plan",
            "plan",
        ]
        for failure in (zlib.error("SECRET_SENTINEL"), OSError("SECRET_SENTINEL")):
            with (
                self.subTest(failure=type(failure)),
                patch.object(sys, "argv", argv),
                patch.object(proof, "proof5", side_effect=failure),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(proof.main(), 1)
                self.assertEqual(output.getvalue(), "PROOF5_REFUSED\n")
        with (
            patch.object(sys, "argv", argv),
            patch.object(proof, "proof5", return_value=proof.empty_outcome(5)),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(proof.main(), 1)
            self.assertEqual(output.getvalue(), "PROOF5_REFUSED\n")

    def test_failed_restore_preserves_capture_and_unknown_actual_observations(
        self,
    ) -> None:
        import tempfile

        from deploy.gate0.evidence import validate
        from scripts.restore_drill import RestoreOutcome, RestoreVerdict

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan, _, _ = write_private_capture(directory)
            for result in (
                RuntimeError("PRIVATE_SENTINEL"),
                RestoreOutcome(
                    RestoreVerdict.FAIL, "bundle_validation", (), False, True, ""
                ),
            ):
                with (
                    self.subTest(result=type(result).__name__),
                    patch.object(
                        proof, "enrolled", return_value=MagicMock(hostname="vmi3537655")
                    ),
                    patch.object(proof.CapturePlan, "load", return_value=plan),
                    patch.object(proof, "pinned_file"),
                    patch.object(proof, "verify_tools"),
                    patch.object(proof, "protected_directory"),
                    patch.object(proof, "protected"),
                    patch.object(
                        proof,
                        "drill_bundle",
                        **(
                            {"side_effect": result}
                            if isinstance(result, Exception)
                            else {"return_value": result}
                        ),
                    ),
                ):
                    outcome = proof.proof5(directory, plan.image, Path("plan"))
                    self.assertEqual(outcome["outcome"], "fail")
                    self.assertIsNotNone(outcome["restore"])
                    restored = outcome["restore"]
                    self.assertIsNone(restored["restored_inventory_sha256"])
                    self.assertEqual(restored["source_identity"], capture.SOURCE)
                    self.assertEqual(restored["capture_sha256"], plan.capture_sha256)
                    if isinstance(result, Exception):
                        self.assertIsNone(restored["source_parity"])
                        self.assertIsNone(restored["scratch_absent"])
                        self.assertEqual(outcome["cleanup"], "fail")
                        self.assertEqual(outcome["failure_code"], "cleanup_failed")
                    else:
                        self.assertFalse(restored["source_parity"])
                        self.assertTrue(restored["scratch_absent"])
                    command = {
                        **plan.context,
                        "command_id": "00000000-0000-4000-8000-000000000002",
                        "proofs": [outcome],
                    }
                    self.assertEqual(validate(command)["proofs"][0], outcome)
                    self.assertNotIn("PRIVATE_SENTINEL", json.dumps(outcome))
