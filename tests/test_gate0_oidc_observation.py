"""Public GitHub API observations are authority; caller booleans cannot pass proof1."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from deploy.agent import contracts as c
from deploy.gate0 import proof1_observation as p1
from deploy.gate0.evidence import GateContext, raw_digest
from tests.test_gate0_evidence_proofs import R, stamp, transition_request


def run_fixture(identifier: int, negative: bool) -> tuple[c.Json, c.Json]:
    current = datetime.now(UTC)

    def when(offset: int) -> str:
        return (current + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    branch = "gate0-negative" if negative else "main"
    shift = 1000 if negative else 0
    run = cast(
        c.Json,
        {
            "id": identifier,
            "run_attempt": 1,
            "workflow_id": 7,
            "head_sha": R,
            "head_branch": branch,
            "actor": {"id": 42},
            "path": p1.WORKFLOW,
            "repository": {"full_name": p1.REPOSITORY},
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "failure" if negative else "success",
            "created_at": when(-2000 + shift),
            "run_started_at": when(-1995 + shift),
            "updated_at": when(-1100 + shift),
        },
    )
    jobs = []
    intervals = [
        (-1900, -1890),
        (-1880, -1870),
        (-1860, -1850),
        (-1850, -1845),
        (-1840, -1830),
        (-1820, -1150),
        (-1140, -1130),
        (-1120, -1110),
    ]
    for index, (key, name) in enumerate(p1.NAMES.items()):
        skipped = not negative and key == "row4"
        failed = negative and key in ("positive", "verdict")
        start, end = intervals[index]
        start, end = start + shift, end + shift
        conclusion = "skipped" if skipped else "failure" if failed else "success"
        steps = (
            []
            if skipped
            else [
                {
                    "number": 2,
                    "name": "Run actions/checkout@v4",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": when(start),
                    "completed_at": when(start + 1),
                },
                {
                    "number": 3,
                    "name": p1.STEP,
                    "status": "completed",
                    "conclusion": conclusion,
                    "started_at": when(start + 1),
                    "completed_at": when(end),
                },
            ]
        )
        jobs.append(
            {
                "id": identifier * 100 + index,
                "run_id": identifier,
                "run_attempt": 1,
                "head_sha": R,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "runner_id": 0 if skipped else 99 if key == "verdict" else 9,
                "runner_name": ""
                if skipped
                else "GitHub Actions"
                if key == "verdict"
                else "Seabone-proof1",
                "labels": ["ubuntu-latest"]
                if key == "verdict"
                else ["self-hosted", "linux", "x64", "dotmac-sub-staging"],
                "started_at": None if skipped else when(start),
                "completed_at": None if skipped else when(end),
                "steps": steps,
            }
        )
    return run, cast(c.Json, {"total_count": len(jobs), "jobs": jobs})


class FakeSource(p1.GitHubProofSource):
    def __init__(self):
        self.runs = {1: run_fixture(1, False), 2: run_fixture(2, True)}
        self.workflow = b"reviewed workflow bytes"
        self.script = b"reviewed script bytes"

    def content(self, path: str, revision: str) -> bytes:
        return self.workflow if path == p1.WORKFLOW else self.script

    def read(self, path: str) -> c.Json:
        identifier = int(path.split("/")[2])
        return copy.deepcopy(self.runs[identifier][1 if "/jobs?" in path else 0])


class OIDCObservationTests(unittest.TestCase):
    def exercise(self, change=None):
        source = FakeSource()
        request = transition_request()
        context = cast(
            GateContext,
            {
                key: value
                for key, value in request.items()
                if key not in ("command_id", "proofs")
            },
        )
        context["action_refs"] = ["oidc-dispatches"]
        plan: p1.OIDCPlan = {
            "action_ref": "oidc-dispatches",
            "not_before": stamp(-2400),
            "workflow_id": 7,
            "workflow_sha256": raw_digest(source.workflow),
            "script_sha256": raw_digest(source.script),
            "runner_id": 9,
            "main": {
                "run_id": 1,
                "attempt": 1,
                "head_sha": R,
                "branch": "main",
                "actor_id": 42,
            },
            "negative": {
                "run_id": 2,
                "attempt": 1,
                "head_sha": R,
                "branch": "gate0-negative",
                "actor_id": 42,
            },
        }
        if change is not None:
            change(source, plan, context)
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / ".runner"
            settings.write_text(
                json.dumps(
                    {
                        "agentId": 9,
                        "agentName": "Seabone-proof1",
                        "gitHubUrl": "https://github.com/" + p1.REPOSITORY,
                    }
                )
            )
            host = SimpleNamespace(
                verify=Mock(),
                enrollment={
                    "runner_settings_sha256": raw_digest(settings.read_bytes())
                },
            )
            with patch.object(p1, "RUNNER_SETTINGS", settings):
                return p1.observe(source, host, plan, context)

    def test_real_owner_wrong_ref_job_not_overall_feature_success(self):
        outcome, main, negative = self.exercise()
        self.assertEqual(outcome["outcome"], "pass")
        self.assertEqual(negative["conclusion"], "failure")
        self.assertTrue(outcome["oidc"]["negative_refused"])
        self.assertEqual(outcome["oidc"]["main_sha256"], c.digest(main))
        self.assertEqual(outcome["oidc"]["negative_sha256"], c.digest(negative))

    def test_scope_drift_never_becomes_public_success(self):
        def mutate(source, plan, context, case):
            main, jobs = source.runs[1]
            if case == "head":
                main["head_sha"] = "f" * 40
            elif case == "actor":
                main["actor"]["id"] = 999
            elif case == "attempt":
                main["run_attempt"] = 2
            elif case == "host":
                jobs["jobs"][0]["runner_id"] = 999
            elif case == "extra_job":
                jobs["total_count"] = 9
            elif case == "stale":
                main["created_at"] = stamp(-9999)
            elif case == "source":
                source.workflow = b"modified feature workflow"
            elif case == "action":
                context["action_refs"] = ["unrelated"]
            elif case == "workflow":
                main["workflow_id"] = 999
            else:
                plan["negative"]["branch"] = "main"

        for case in (
            "head",
            "actor",
            "attempt",
            "host",
            "extra_job",
            "stale",
            "source",
            "action",
            "workflow",
            "wrong_ref",
        ):
            with self.subTest(case=case), self.assertRaises(c.ContractError):
                self.exercise(
                    lambda source, plan, context, case=case: mutate(
                        source, plan, context, case
                    )
                )

    def test_skipped_or_failed_proof_step_is_honest_terminal_fail(self):
        def change(source, plan, context):
            source.runs[2][1]["jobs"][3]["steps"][1]["conclusion"] = "failure"
            source.runs[2][1]["jobs"][3]["conclusion"] = "failure"

        outcome, _, _ = self.exercise(change)
        self.assertEqual(outcome["outcome"], "fail")
        self.assertEqual(outcome["failure_code"], "observation_mismatch")
        self.assertFalse(outcome["oidc"]["negative_refused"])

    def test_main_positive_success_does_not_substitute_for_wrong_ref(self):
        def change(source, plan, context):
            source.runs[2][1]["jobs"][3]["steps"][1]["name"] = (
                "unrelated successful command"
            )

        outcome, _, _ = self.exercise(change)
        self.assertEqual(outcome["outcome"], "fail")
        self.assertTrue(outcome["oidc"]["main_accepted"])
        self.assertFalse(outcome["oidc"]["negative_refused"])

    def test_expired_jwt_row_cannot_skip_its_owner_wait(self):
        def change(source, plan, context):
            row = source.runs[1][1]["jobs"][5]
            row["steps"][1]["completed_at"] = stamp(-1800)

        with self.assertRaises(c.ContractError):
            self.exercise(change)

    def test_authenticated_source_refuses_redirect_and_unbounded_payload(self):
        source = p1.GitHubProofSource(token="fixture")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("private response")
            with self.assertRaisesRegex(
                ValueError, "^github_proof_evidence_unavailable$"
            ):
                source.read("actions/runs/1")
        with patch("urllib.request.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"x" * 1048577
            with self.assertRaisesRegex(
                ValueError, "^github_proof_evidence_unavailable$"
            ):
                source.read("actions/runs/1")
            response.read.assert_called_once_with(1048577)
        for path in (
            "https://other.invalid",
            "actions/runs/1/logs",
            "actions/runs/1/dispatches",
        ):
            with self.subTest(path=path), self.assertRaises(c.ContractError):
                source.read(path)
