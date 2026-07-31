from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.ci.check_promotion_policy import (
    VERSION_BUMP_FILES,
    PromotionEvidence,
    PromotionKind,
    PromotionPolicyViolation,
    evaluate_promotion,
)

HEAD_SHA = "a" * 40


def _staged_release() -> PromotionEvidence:
    return PromotionEvidence(
        base_ref="main",
        base_repository="michaelayoade/dotmac_sub",
        head_ref="release/promote-7-71-0",
        head_repository="michaelayoade/dotmac_sub",
        head_sha=HEAD_SHA,
        author_login="release-operator",
        labels=frozenset({"version:none"}),
        body=f"Staged dev SHA: `{HEAD_SHA}`",
        changed_files=frozenset({"app/example.py"}),
        dev_contains_head=True,
        staging_succeeded=True,
        approval_count=0,
    )


def test_exact_staged_dev_commit_can_promote_to_main() -> None:
    assert evaluate_promotion(_staged_release()) is PromotionKind.STAGED_RELEASE


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("dev_contains_head", False, "not contained in origin/dev"),
        ("staging_succeeded", False, "no latest successful staging deployment"),
        ("body", "Staged dev SHA: `bbbb`", "must declare `Staged dev SHA"),
        ("labels", frozenset({"version:patch"}), "version:none"),
    ],
)
def test_staged_release_fails_closed_on_missing_evidence(
    field: str,
    value: object,
    expected_error: str,
) -> None:
    with pytest.raises(PromotionPolicyViolation, match=expected_error):
        evaluate_promotion(replace(_staged_release(), **{field: value}))


def test_generated_main_version_bump_is_narrowly_allowlisted() -> None:
    evidence = replace(
        _staged_release(),
        head_ref="automation/version-bump-main",
        body="",
        changed_files=VERSION_BUMP_FILES,
        dev_contains_head=False,
        staging_succeeded=False,
    )

    assert evaluate_promotion(evidence) is PromotionKind.GENERATED_VERSION_BUMP


def test_generated_main_version_bump_rejects_an_unexpected_file() -> None:
    evidence = replace(
        _staged_release(),
        head_ref="automation/version-bump-main",
        body="",
        changed_files=VERSION_BUMP_FILES | {"app/backdoor.py"},
        dev_contains_head=False,
        staging_succeeded=False,
    )

    with pytest.raises(PromotionPolicyViolation, match="changed other files"):
        evaluate_promotion(evidence)


def test_reviewed_incident_hotfix_can_bypass_dev() -> None:
    evidence = replace(
        _staged_release(),
        head_ref="hotfix/restore-customer-auth",
        labels=frozenset({"hotfix", "version:patch"}),
        body=(
            "Incident: INC-2026-0730\n"
            "Why dev was bypassed: active customer authentication incident\n"
            "Back-sync plan: automatic main-to-dev reconciliation PR\n"
        ),
        dev_contains_head=False,
        staging_succeeded=False,
        approval_count=1,
    )

    assert evaluate_promotion(evidence) is PromotionKind.EMERGENCY_HOTFIX


def test_hotfix_label_alone_is_not_authorization() -> None:
    evidence = replace(
        _staged_release(),
        head_ref="hotfix/restore-customer-auth",
        labels=frozenset({"hotfix", "version:patch"}),
        body="Incident: INC-2026-0730",
        dev_contains_head=False,
        staging_succeeded=False,
        approval_count=0,
    )

    with pytest.raises(PromotionPolicyViolation) as exc_info:
        evaluate_promotion(evidence)

    assert any("requires approval" in error for error in exc_info.value.errors)
    assert any("Why dev was bypassed" in error for error in exc_info.value.errors)
    assert any("Back-sync plan" in error for error in exc_info.value.errors)


def test_ordinary_feature_branch_cannot_target_main() -> None:
    evidence = replace(
        _staged_release(),
        head_ref="feature/direct-main-change",
        labels=frozenset({"version:minor"}),
    )

    with pytest.raises(PromotionPolicyViolation, match="main accepts only"):
        evaluate_promotion(evidence)
