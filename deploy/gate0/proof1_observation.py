"""Read-only proof1 export from authenticated GitHub run/attempt/job authority."""

from __future__ import annotations

import base64
import re
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

from deploy.agent import contracts as c
from deploy.gate0.evidence import (
    GateContext,
    NoRedirect,
    ProofOutcome,
    empty_outcome,
    raw_digest,
)
from deploy.gate0.host_evidence import EnrolledHost
from scripts.verify_github_release import GITHUB_API_ROOT, GitHubActionsSource

REPOSITORY = "michaelayoade/dotmac_sub"
WORKFLOW = ".github/workflows/openbao-jwt-proof.yml"
SCRIPT = "deploy/gate0/proof1_oidc_openbao.sh"
STEP = "Run set -euo pipefail"
RUNNER_SETTINGS = Path("/opt/dotmac-proof1-runner/.runner")
NAMES = {
    "positive": "Rows 1+8+9: staging login, canary OK, prod/sys/role 403",
    "row2": "Row 2: production env token to sub-staging-ci refused",
    "row3": "Row 3: no environment claim refused",
    "row4": "Row 4: non-main ref refused (branch dispatches only)",
    "row5": "Row 5: proof-workflow token to sub-staging-ci refused",
    "row6": "Row 6: expired GitHub JWT refused",
    "row7": "Row 7: wrong audience refused",
    "verdict": "Gate 0 proof 1 verdict",
}
Conclusion = Literal[
    "success",
    "failure",
    "skipped",
    "cancelled",
    "timed_out",
    "action_required",
    "neutral",
    "stale",
]


class ExpectedRun(TypedDict):
    run_id: int
    attempt: int
    head_sha: c.Revision
    branch: c.BoundedText
    actor_id: int


class OIDCPlan(TypedDict):
    action_ref: c.Identifier
    not_before: str
    workflow_id: int
    workflow_sha256: c.Digest
    script_sha256: c.Digest
    runner_id: int
    main: ExpectedRun
    negative: ExpectedRun


class StepFact(TypedDict):
    number: int
    name: c.BoundedText
    status: Literal["completed"]
    conclusion: Conclusion
    started_at: str
    completed_at: str


class JobFact(TypedDict):
    job_id: int
    run_id: int
    attempt: int
    head_sha: c.Revision
    name: c.BoundedText
    status: Literal["completed"]
    conclusion: Conclusion
    runner_id: int | None
    runner_name: c.BoundedText | None
    labels: list[c.Identifier]
    started_at: str | None
    completed_at: str | None
    steps: list[StepFact]


class RunFact(TypedDict):
    run_id: int
    attempt: int
    workflow_id: int
    head_sha: c.Revision
    branch: c.BoundedText
    actor_id: int
    path: Literal[".github/workflows/openbao-jwt-proof.yml"]
    repository: Literal["michaelayoade/dotmac_sub"]
    event: Literal["workflow_dispatch"]
    status: Literal["completed"]
    conclusion: Conclusion
    created_at: str
    started_at: str
    completed_at: str
    jobs: list[JobFact]


class GitHubProofSource(GitHubActionsSource):
    """The existing GitHub read owner's credentials and clock, fixed queries only."""

    def read(self, path: str) -> c.Json:
        c.require(
            self._token is not None
            and re.fullmatch(r"[A-Za-z0-9._~-]+", self._token) is not None
        )
        allowed = (
            r"actions/runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*/jobs\?per_page=100)?"
            r"|contents/(?:\.github/workflows/openbao-jwt-proof\.yml|deploy/gate0/proof1_oidc_openbao\.sh)\?ref=[0-9a-f]{40}"
        )
        c.require(
            re.fullmatch(allowed, path) is not None and 0 < self._timeout_seconds <= 30
        )
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS GitHub API owner
            GITHUB_API_ROOT + "/repos/" + REPOSITORY + "/" + path,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "dotmac-sub-gate0-proof1",
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(1048577)
                c.require(len(raw) <= 1048576)
                return c.read_json(raw)
        except (OSError, ValueError):
            raise ValueError("github_proof_evidence_unavailable") from None

    def content(self, path: str, revision: str) -> bytes:
        c.require(path in (WORKFLOW, SCRIPT))
        c.sha(revision, prefix=False)
        value = self.read("contents/" + path + "?ref=" + revision)
        c.require(
            value["type"] == "file"
            and value["path"] == path
            and value["encoding"] == "base64"
        )
        size = value["size"]
        c.require(type(size) is int and 0 < size <= 65536)
        raw = base64.b64decode(
            cast(str, value["content"]).replace("\n", ""), validate=True
        )
        c.require(len(raw) == size)
        return raw


def normalized_step(raw: c.Json) -> StepFact:
    result = cast(StepFact, {key: raw[key] for key in StepFact.__annotations__})
    c._shape(result, StepFact)
    c.require(result["number"] > 0)
    return result


def normalized_job(raw: c.Json) -> JobFact:
    result = cast(
        JobFact,
        {
            "job_id": raw["id"],
            "run_id": raw["run_id"],
            "attempt": raw["run_attempt"],
            "head_sha": raw["head_sha"],
            "name": raw["name"],
            "status": raw["status"],
            "conclusion": raw["conclusion"],
            "runner_id": raw["runner_id"],
            "runner_name": raw["runner_name"] or None,
            "labels": raw["labels"],
            "started_at": raw["started_at"],
            "completed_at": raw["completed_at"],
            "steps": [
                normalized_step(cast(c.Json, step))
                for step in cast(list[c.Json], raw["steps"])
            ],
        },
    )
    c._shape(result, JobFact)
    c.require(
        result["job_id"] > 0
        and len(result["steps"]) <= 32
        and len(result["labels"]) <= 16
    )
    c.require(len({step["number"] for step in result["steps"]}) == len(result["steps"]))
    result["steps"].sort(key=lambda step: step["number"])
    return result


def normalize_run(raw: c.Json, jobs: c.Json) -> RunFact:
    result = cast(
        RunFact,
        {
            "run_id": raw["id"],
            "attempt": raw["run_attempt"],
            "workflow_id": raw["workflow_id"],
            "head_sha": raw["head_sha"],
            "branch": raw["head_branch"],
            "actor_id": cast(c.Json, raw["actor"])["id"],
            "path": raw["path"],
            "repository": cast(c.Json, raw["repository"])["full_name"],
            "event": raw["event"],
            "status": raw["status"],
            "conclusion": raw["conclusion"],
            "created_at": raw["created_at"],
            "started_at": raw["run_started_at"],
            "completed_at": raw["updated_at"],
            "jobs": [
                normalized_job(cast(c.Json, job))
                for job in cast(list[c.Json], jobs["jobs"])
            ],
        },
    )
    c._shape(result, RunFact)
    c.require(
        type(jobs["total_count"]) is int
        and jobs["total_count"] == len(result["jobs"]) == len(NAMES)
    )
    c.require(
        len({job["job_id"] for job in result["jobs"]}) == len(NAMES)
        and {job["name"] for job in result["jobs"]} == set(NAMES.values())
    )
    result["jobs"].sort(key=lambda job: job["job_id"])
    return result


def validate_binding(
    fact: RunFact, expected: ExpectedRun, plan: OIDCPlan, context: GateContext
) -> None:
    for field, key in (
        ("run_id", "run_id"),
        ("attempt", "attempt"),
        ("head_sha", "head_sha"),
        ("branch", "branch"),
        ("actor_id", "actor_id"),
    ):
        c.require(fact[field] == expected[key])
    c.require(
        fact["workflow_id"] == plan["workflow_id"]
        and expected["run_id"] > 0
        and expected["attempt"] > 0
        and expected["actor_id"] > 0
    )
    first, last = c.timestamp(plan["not_before"]), c.timestamp(context["deadline"])
    c.require(timedelta(0) < last - first <= timedelta(hours=24))
    start, finish = c.timestamp(fact["started_at"]), c.timestamp(fact["completed_at"])
    c.require(first <= c.timestamp(fact["created_at"]) <= start <= finish <= last)
    for job in fact["jobs"]:
        c.require(
            job["run_id"] == fact["run_id"]
            and job["attempt"] == fact["attempt"]
            and job["head_sha"] == fact["head_sha"]
        )
        if job["conclusion"] == "skipped":
            c.require(job["name"] == NAMES["row4"] and not job["steps"])
            continue
        c.require(job["started_at"] is not None and job["completed_at"] is not None)
        job_start, job_end = (
            c.timestamp(job["started_at"]),
            c.timestamp(job["completed_at"]),
        )
        c.require(start <= job_start <= job_end <= finish)
        if job["name"] != NAMES["verdict"]:
            c.require(
                job["runner_id"] == plan["runner_id"]
                and job["runner_name"] == "Seabone-proof1"
                and {"self-hosted", "linux", "x64", "dotmac-sub-staging"}
                <= set(job["labels"])
            )
        for step in job["steps"]:
            if (
                job["name"] == NAMES["row6"]
                and step["name"] == STEP
                and step["conclusion"] == "success"
            ):
                c.require(
                    (
                        c.timestamp(step["completed_at"])
                        - c.timestamp(step["started_at"])
                    ).total_seconds()
                    >= 660
                )
            c.require(
                job_start
                <= c.timestamp(step["started_at"])
                <= c.timestamp(step["completed_at"])
                <= job_end
            )


def proof_step(job: JobFact) -> bool:
    matches = [step for step in job["steps"] if step["name"] == STEP]
    checkout = [
        step for step in job["steps"] if step["name"] == "Run actions/checkout@v4"
    ]
    return (
        len(checkout) == 1
        and checkout[0]["conclusion"] == "success"
        and len(matches) == 1
        and matches[0]["conclusion"] == "success"
        and job["conclusion"] == "success"
    )


def observe(
    source: GitHubProofSource, host: EnrolledHost, plan: OIDCPlan, context: GateContext
) -> tuple[ProofOutcome, RunFact, RunFact]:
    c._shape(plan, OIDCPlan)
    c._shape(context, GateContext)
    c.require(
        plan["action_ref"] in context["action_refs"]
        and plan["main"]["branch"] == "main"
        and plan["negative"]["branch"] != "main"
        and plan["main"]["run_id"] != plan["negative"]["run_id"]
    )
    c.require(context["phase"] == "initial" and plan["workflow_id"] > 0)
    settings_path = RUNNER_SETTINGS
    host.verify(settings_path, host.enrollment["runner_settings_sha256"])
    c.require(settings_path.stat().st_size <= 65536)
    settings = c.read_json(settings_path.read_bytes())
    c.require(
        type(settings["agentId"]) is int
        and settings["agentId"] == plan["runner_id"] > 0
        and settings["agentName"] == "Seabone-proof1"
        and settings["gitHubUrl"] == "https://github.com/" + REPOSITORY
    )
    facts = []
    for expected in (plan["main"], plan["negative"]):
        c.require(
            raw_digest(source.content(WORKFLOW, expected["head_sha"]))
            == plan["workflow_sha256"]
            and raw_digest(source.content(SCRIPT, expected["head_sha"]))
            == plan["script_sha256"]
        )
        path = "actions/runs/" + str(expected["run_id"])
        raw = source.read(path)
        fact = normalize_run(
            raw,
            source.read(
                path + "/attempts/" + str(expected["attempt"]) + "/jobs?per_page=100"
            ),
        )
        validate_binding(fact, expected, plan, context)
        reread = source.read(path)
        reread_jobs = source.read(
            path + "/attempts/" + str(expected["attempt"]) + "/jobs?per_page=100"
        )
        c.require(normalize_run(reread, reread_jobs) == fact)
        facts.append(fact)
    main, negative = facts
    main_jobs = {job["name"]: job for job in main["jobs"]}
    negative_jobs = {job["name"]: job for job in negative["jobs"]}
    main_ok = (
        main["conclusion"] == "success"
        and main_jobs[NAMES["row4"]]["conclusion"] == "skipped"
    )
    main_ok &= all(
        proof_step(main_jobs[name])
        for key, name in NAMES.items()
        if key not in ("row4", "verdict")
    )
    main_ok &= main_jobs[NAMES["verdict"]]["conclusion"] == "success"
    negative_ok = proof_step(negative_jobs[NAMES["row4"]])
    # The owner's positive/aggregate jobs fail on the wrong ref by design.
    negative_ok &= (
        negative["conclusion"] == "failure"
        and negative_jobs[NAMES["positive"]]["conclusion"] == "failure"
        and negative_jobs[NAMES["verdict"]]["conclusion"] == "failure"
    )
    outcome = empty_outcome(1)
    outcome.update(
        cleanup="pass",
        failure_code="observation_mismatch",
        oidc={
            "main_run_id": main["run_id"],
            "negative_run_id": negative["run_id"],
            "main_sha256": c.digest(main),
            "negative_sha256": c.digest(negative),
            "main_accepted": bool(main_ok),
            "negative_refused": bool(negative_ok),
        },
    )
    if main_ok and negative_ok:
        outcome.update(outcome="pass", failure_code="none")
    return outcome, main, negative
