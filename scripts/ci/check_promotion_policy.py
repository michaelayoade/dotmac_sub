#!/usr/bin/env python3
"""Fail-closed policy for pull requests targeting the production branch."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from enum import StrEnum

VERSION_LABELS = frozenset(
    {"version:major", "version:minor", "version:patch", "version:none"}
)
VERSION_BUMP_FILES = frozenset(
    {
        "CHANGELOG.md",
        "VERSION",
        "mobile/lib/main.dart",
        "mobile/lib/src/config/env.dart",
        "mobile/pubspec.yaml",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
    }
)


class PromotionKind(StrEnum):
    STAGED_RELEASE = "staged_release"
    GENERATED_VERSION_BUMP = "generated_version_bump"
    EMERGENCY_HOTFIX = "emergency_hotfix"


@dataclass(frozen=True)
class PromotionEvidence:
    base_ref: str
    base_repository: str
    head_ref: str
    head_repository: str
    head_sha: str
    author_login: str
    labels: frozenset[str]
    body: str
    changed_files: frozenset[str]
    dev_contains_head: bool
    staging_succeeded: bool
    approval_count: int


class PromotionPolicyViolation(Exception):
    """Raised when a main-targeting pull request lacks required evidence."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _body_field(body: str, field: str) -> str | None:
    match = re.search(
        rf"(?im)^{re.escape(field)}:\s*`?([^`\r\n]+?)`?\s*$",
        body,
    )
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _single_version_label(evidence: PromotionEvidence, expected: str) -> list[str]:
    errors: list[str] = []
    selected = evidence.labels & VERSION_LABELS
    if selected != {expected}:
        errors.append(
            f"expected exactly the {expected!r} label; found {sorted(selected)!r}"
        )
    return errors


def _evaluate_staged_release(evidence: PromotionEvidence) -> list[str]:
    errors = _single_version_label(evidence, "version:none")
    if not evidence.dev_contains_head:
        errors.append("the promotion head SHA is not contained in origin/dev")
    if not evidence.staging_succeeded:
        errors.append(
            "the promotion head SHA has no latest successful staging deployment"
        )

    declared_sha = _body_field(evidence.body, "Staged dev SHA")
    if declared_sha != evidence.head_sha:
        errors.append(
            "the PR body must declare `Staged dev SHA: <head SHA>` for the exact head"
        )
    return errors


def _evaluate_generated_version_bump(evidence: PromotionEvidence) -> list[str]:
    errors = _single_version_label(evidence, "version:none")
    if evidence.changed_files != VERSION_BUMP_FILES:
        missing = sorted(VERSION_BUMP_FILES - evidence.changed_files)
        unexpected = sorted(evidence.changed_files - VERSION_BUMP_FILES)
        if missing:
            errors.append(f"generated version bump is missing files: {missing!r}")
        if unexpected:
            errors.append(f"generated version bump changed other files: {unexpected!r}")
    return errors


def _evaluate_emergency_hotfix(evidence: PromotionEvidence) -> list[str]:
    errors = _single_version_label(evidence, "version:patch")
    if "hotfix" not in evidence.labels:
        errors.append("an emergency main bypass requires the hotfix label")
    if evidence.approval_count < 1:
        errors.append(
            "an emergency hotfix requires approval from someone other than its author"
        )
    for field in ("Incident", "Why dev was bypassed", "Back-sync plan"):
        if _body_field(evidence.body, field) is None:
            errors.append(f"the PR body must include a non-empty `{field}: ...` line")
    return errors


def evaluate_promotion(evidence: PromotionEvidence) -> PromotionKind:
    """Return the allowed promotion class or fail with all policy violations."""

    common_errors: list[str] = []
    if evidence.base_ref != "main":
        common_errors.append(
            "promotion policy only accepts pull requests targeting main"
        )
    if evidence.head_repository != evidence.base_repository:
        common_errors.append(
            "main-targeting pull requests must originate in this repository"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", evidence.head_sha):
        common_errors.append("head SHA must be a full lowercase Git object ID")

    if evidence.head_ref.startswith("release/promote-"):
        kind = PromotionKind.STAGED_RELEASE
        errors = _evaluate_staged_release(evidence)
    elif evidence.head_ref == "automation/version-bump-main":
        kind = PromotionKind.GENERATED_VERSION_BUMP
        errors = _evaluate_generated_version_bump(evidence)
    elif evidence.head_ref.startswith("hotfix/"):
        kind = PromotionKind.EMERGENCY_HOTFIX
        errors = _evaluate_emergency_hotfix(evidence)
    else:
        kind = PromotionKind.STAGED_RELEASE
        errors = [
            "main accepts only release/promote-*, automation/version-bump-main, "
            "or hotfix/* pull requests"
        ]

    all_errors = tuple(common_errors + errors)
    if all_errors:
        raise PromotionPolicyViolation(all_errors)
    return kind


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _string_set(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return frozenset(value)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def evidence_from_json(raw: str) -> PromotionEvidence:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("promotion evidence must be a JSON object")
    return PromotionEvidence(
        base_ref=_string(payload.get("baseRef"), "baseRef"),
        base_repository=_string(payload.get("baseRepository"), "baseRepository"),
        head_ref=_string(payload.get("headRef"), "headRef"),
        head_repository=_string(payload.get("headRepository"), "headRepository"),
        head_sha=_string(payload.get("headSha"), "headSha"),
        author_login=_string(payload.get("authorLogin"), "authorLogin"),
        labels=_string_set(payload.get("labels"), "labels"),
        body=_string(payload.get("body"), "body"),
        changed_files=_string_set(payload.get("changedFiles"), "changedFiles"),
        dev_contains_head=_boolean(payload.get("devContainsHead"), "devContainsHead"),
        staging_succeeded=_boolean(payload.get("stagingSucceeded"), "stagingSucceeded"),
        approval_count=_integer(payload.get("approvalCount"), "approvalCount"),
    )


def main() -> int:
    raw = os.environ.get("PROMOTION_EVIDENCE_JSON", "")
    if not raw:
        print("error: PROMOTION_EVIDENCE_JSON is required", file=sys.stderr)
        return 1

    try:
        evidence = evidence_from_json(raw)
        kind = evaluate_promotion(evidence)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: invalid promotion evidence: {exc}", file=sys.stderr)
        return 1
    except PromotionPolicyViolation as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Promotion policy accepted: {kind.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
