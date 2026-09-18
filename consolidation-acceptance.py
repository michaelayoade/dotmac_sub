"""Read-only development evidence collector; never included in the source PR."""
from pathlib import Path
import json
import os
import re
import subprocess
import time

REPO = "michaelayoade/dotmac_sub"
HEAD = os.environ["EXPECTED_HEAD"]
BASE = "0ba2f8a7228219928b3e24296758dc8ab995313c"
OUT = Path("/tmp/consolidation-acceptance")
OUT.mkdir(exist_ok=True)
INCLUDED = [3178, 3179, 3180, 3181, 3182, 3183, 3185]
EXCLUDED = [3155, 3184]
WORKFLOWS = {"CI", "E2E Gate", "Engineering standards", "Version Impact", "Release Freeze Gate", "Mobile CI"}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def api(path: str, *, text: bool = False):
    command = ["gh", "api"]
    if text:
        command.append("--allow-escape-sequences")
    result = subprocess.check_output([*command, f"repos/{REPO}/{path}"], text=True)
    return result if text else json.loads(result)


def save(name: str, value) -> None:
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n")


def validate_sources() -> None:
    assert git("rev-parse", "HEAD") == HEAD
    pr = api("pulls/3186")
    assert pr["head"]["sha"] == HEAD, "Consolidated PR head moved"
    assert pr["base"]["ref"] == "main"
    assert api("git/ref/heads/main")["object"]["sha"] == BASE, "Main moved; revalidate its integration"
    save("consolidated-pr.json", {k: pr[k] for k in ["number", "title", "state", "draft", "mergeable", "mergeable_state", "additions", "deletions", "changed_files"]})
    added = set(git("rev-list", HEAD, f"^{BASE}").splitlines())
    sources = []
    for number in INCLUDED + EXCLUDED:
        source = api(f"pulls/{number}")
        sha = source["head"]["sha"]
        present = subprocess.run(["git", "merge-base", "--is-ancestor", sha, HEAD]).returncode == 0
        unique = set(git("rev-list", sha, f"^{BASE}").splitlines())
        sources.append({"number": number, "title": source["title"], "head_sha": sha, "state": source["state"], "included": number in INCLUDED, "is_ancestor": present, "unique_commits_in_consolidation": sorted(unique & added)})
        save("source-preservation.json", sources)
        if number in INCLUDED:
            assert present, f"Source PR #{number} changed or is not fully preserved"
        else:
            assert not (unique & added), f"Excluded PR #{number} contributes unique commits"
    assert not git("diff", "--name-only", BASE, HEAD, "--", "VERSION", ".github/workflows"), "Protected release/CI files changed"
    save("preservation-result.json", {"head": HEAD, "base": BASE, "all_source_heads_preserved": True, "excluded_unique_commits_absent": True, "release_and_ci_files_unchanged": True})


def collect_logs(runs: list[dict]) -> None:
    jobs = []
    for run in runs:
        if run["name"] not in WORKFLOWS:
            continue
        batch = api(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        for job in batch:
            jobs.append({"workflow": run["name"], "run_id": run["id"], "job_id": job["id"], "name": job["name"], "status": job["status"], "conclusion": job["conclusion"]})
            if job["status"] != "completed" or job["conclusion"] in {"skipped", "cancelled"}:
                continue
            try:
                text = re.sub(r"\x1b\[[0-9;]*m", "", api(f"actions/jobs/{job['id']}/logs", text=True))
            except subprocess.CalledProcessError:
                continue
            lines = text.splitlines()
            selected = [line for line in lines if re.search(r"\d+ passed|\d+ failed|ERROR collecting|All checks passed|files already formatted|Success: no issues|[0-9]+ contracts kept|##\[error\]", line)]
            if job["conclusion"] == "failure":
                collecting = False
                for line in lines:
                    if "FAILURES" in line or "ERRORS" in line:
                        collecting = True
                    if collecting:
                        selected.append(line)
                    if collecting and ("warnings summary" in line or "short test summary" in line):
                        collecting = False
                selected.extend(lines[-70:])
            (OUT / f"job-{job['id']}.txt").write_text("\n".join(selected) + "\n")
    save("jobs.json", jobs)


validate_sources()
start = time.monotonic()
runs = []
while time.monotonic() - start < 1800:
    latest = {}
    for run in api(f"actions/runs?head_sha={HEAD}&event=pull_request&per_page=100")["workflow_runs"]:
        if run["name"] in WORKFLOWS and run["name"] not in latest:
            latest[run["name"]] = run
    runs = list(latest.values())
    report = [{k: run[k] for k in ["id", "name", "status", "conclusion", "head_sha", "run_attempt", "html_url"]} for run in runs]
    save("workflows.json", report)
    print(json.dumps(report), flush=True)
    if any(run["status"] == "completed" and run["conclusion"] not in {"success", "skipped", "neutral"} for run in runs):
        collect_logs(runs)
        raise SystemExit("A required workflow failed or was cancelled; inspect evidence")
    if WORKFLOWS <= latest.keys() and all(run["status"] == "completed" and run["conclusion"] == "success" for run in runs):
        validate_sources()
        checks = api(f"commits/{HEAD}/check-runs?per_page=100")["check_runs"]
        newest = {}
        for check in checks:
            if check["name"] not in newest:
                newest[check["name"]] = check
        save("check-runs.json", [{k: check[k] for k in ["id", "name", "status", "conclusion", "head_sha", "details_url"]} for check in newest.values()])
        assert len(newest) >= 19, "Expected repository checks are missing"
        assert all(check["status"] == "completed" and check["conclusion"] in {"success", "skipped", "neutral"} for check in newest.values()), "Not all exact-head checks are complete and passing"
        collect_logs(runs)
        save("acceptance-result.json", {"head": HEAD, "all_six_workflows_succeeded": True, "check_count": len(newest), "source_preservation_verified": True})
        print("ALL EXACT-HEAD WORKFLOWS SUCCEEDED; SOURCE PRESERVATION VERIFIED", flush=True)
        break
    time.sleep(30)
else:
    collect_logs(runs)
    raise SystemExit("Checks are still pending; no all-green acceptance recorded")
