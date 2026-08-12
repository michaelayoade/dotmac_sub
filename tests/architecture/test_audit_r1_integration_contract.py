"""The checked R1 candidate says exactly what is and is not released."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "docs/audits/audit-r1-kernel-candidate.json"
RUNBOOK = ROOT / "docs/audits/AUDIT_R1_KERNEL_INTEGRATION.md"


def _candidate() -> dict[str, object]:
    return json.loads(CANDIDATE.read_text(encoding="utf-8"))


def test_unreleased_candidate_is_not_claimed_as_the_sub_pin() -> None:
    candidate = _candidate()
    kernel = candidate["kernel"]
    sub = candidate["sub"]

    assert candidate["status"] == "candidate_not_released"
    assert kernel["candidate_version"] == "0.1.0a42"
    assert len(kernel["commit"]) == 40
    assert len(kernel["wheel_sha256"]) == 64
    assert len(sub["implementation_commit"]) == 40
    assert sub["released_kernel_pin"] == "0.1.0a40"

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dotmac-kernel==0.1.0a40" in pyproject
    assert "dotmac-kernel==0.1.0a42" not in pyproject


def test_runbook_preserves_the_expansion_and_release_boundaries() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    required = (
        "authored together on integration branches",
        "cannot be released atomically",
        "created_at timestamptz NULL",
        "DEFAULT now()",
        "counts only",
        "not be described as kernel-lineage adoption",
    )
    assert all(phrase in runbook for phrase in required)
