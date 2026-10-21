"""Offline scratch lifecycle tests; every process boundary is mocked."""

import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts import restore_drill as drill


def result(stdout: bytes = b"", code: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], code, stdout, b"private SQL payload")


class ScratchCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pg = drill.ScratchPostgres("postgres:16-alpine")
        self.container_id = "a" * 64
        self.owned = result(
            f"{self.container_id}|{self.pg._ownership_label}\n".encode()
        )

    def test_success_removes_by_owned_id_and_verifies_absence(self) -> None:
        with patch.object(
            drill, "run", side_effect=[self.owned, result(), result()]
        ) as run:
            self.pg.__exit__()
        self.assertEqual(
            run.call_args_list[1].args[0], ["docker", "rm", "-f", self.container_id]
        )
        self.assertEqual(run.call_args_list[0], run.call_args_list[2])

    def test_already_absent_is_safe(self) -> None:
        with patch.object(drill, "run", return_value=result()) as run:
            self.pg.__exit__()
        self.assertEqual(run.call_count, 1)

    def test_unowned_collision_is_never_removed(self) -> None:
        for record in (f"{self.container_id}|another-run", "malformed"):
            with (
                self.subTest(record=record),
                patch.object(drill, "run", return_value=result(record.encode())) as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "ownership unproved"):
                    self.pg.__exit__()
                self.assertEqual(run.call_count, 1)

    def test_failed_or_timed_out_remove_still_checks_absence_and_fails(self) -> None:
        for removal in (result(code=1), subprocess.TimeoutExpired("docker", 60)):
            with (
                self.subTest(removal=removal),
                patch.object(
                    drill, "run", side_effect=[self.owned, removal, result()]
                ) as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    self.pg.__exit__()
                self.assertEqual(run.call_count, 3)

    def test_surviving_resource_fails(self) -> None:
        with patch.object(drill, "run", side_effect=[self.owned, result(), self.owned]):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                self.pg.__exit__()

    def test_presence_errors_are_sanitized(self) -> None:
        for observation in (result(code=1), OSError("private SQL payload")):
            with (
                self.subTest(observation=observation),
                patch.object(drill, "run", side_effect=[observation]),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "^scratch presence query failed$"
                ):
                    self.pg.__exit__()

    def test_verification_failure_cannot_succeed(self) -> None:
        with patch.object(
            drill, "run", side_effect=[self.owned, result(), result(code=1)]
        ):
            with self.assertRaisesRegex(RuntimeError, "presence query failed"):
                self.pg.__exit__()

    def test_startup_failure_and_timeout_clean_partial_resource(self) -> None:
        for startup in (result(code=1), subprocess.TimeoutExpired("docker", 120)):
            with (
                self.subTest(startup=startup),
                patch.object(
                    drill, "run", side_effect=[startup, self.owned, result(), result()]
                ) as run,
            ):
                with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                    self.pg.__enter__()
                self.assertEqual(run.call_count, 4)
                self.assertIn(
                    f"dotmac.restore-drill.run={self.pg._ownership_label}",
                    run.call_args_list[0].args[0],
                )

    def test_readiness_failure_cleans(self) -> None:
        with (
            patch.object(
                drill, "run", side_effect=[result(), self.owned, result(), result()]
            ),
            patch.object(self.pg, "psql", return_value=result(code=1)),
            patch.object(drill.time, "time", side_effect=[0, 100]),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                self.pg.__enter__()

    def test_body_failure_still_cleans_and_propagates(self) -> None:
        with (
            patch.object(drill, "run", side_effect=[self.owned, result(), result()]),
            patch.object(self.pg, "_start", return_value=self.pg),
        ):
            with self.assertRaisesRegex(ValueError, "assertion failed"):
                with self.pg:
                    raise ValueError("assertion failed")

    def test_ready_instance_uses_one_cleanup_path(self) -> None:
        cleanup = Mock()
        with (
            patch.object(self.pg, "_start", return_value=self.pg),
            patch.object(self.pg, "_cleanup", cleanup),
        ):
            with self.pg as pg:
                self.assertIs(pg, self.pg)
        cleanup.assert_called_once_with()
