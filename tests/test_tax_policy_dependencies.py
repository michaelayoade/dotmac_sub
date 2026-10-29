"""Typed dependency authoring and evidence requirements; no policy arithmetic."""

from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from dotmac_tax import TaxNotFound, TaxRuleInputV2, TaxRuleViolation
from sqlalchemy.orm import Session

from app.services import tax_policy_authoring as owner
from app.services.permission_admission import PermissionAdmission
from app.services.tax_policy_authoring_contracts import (
    DependencySelection,
    PolicyIdentity,
    PreviewDraft,
    RuleDraft,
)
from app.services.tax_policy_authoring_workbench import WorkbenchQuery


def _rule(*, selection: DependencySelection = DependencySelection.none) -> RuleDraft:
    return RuleDraft(
        jurisdiction_id=uuid4(),
        jurisdiction_code="JUR",
        tax_code="CODE",
        tax_code_id=uuid4(),
        version=1,
        effective_from=date(2026, 1, 1),
        priority=100,
        fact_kind="expected_withholding",
        recognition_basis_code="invoice_expectation",
        transaction_side="withholding",
        calculation_method="percentage",
        rate=Decimal("0.1"),
        currency_code="NGN",
        minor_units=2,
        inclusive=False,
        recoverable_rate=Decimal("0"),
        treatment_code="standard_rated",
        calculation_sequence=100,
        calculation_base_code="source_amount"
        if selection is DependencySelection.none
        else "source_plus_dependency_tax",
        dependency_selection=selection,
        reason="Approved test policy",
        evidence_ref="test:decision",
        source_ref="test:source",
        source_version="1",
    )


def _preview(
    *, mode: str = "dependency", prior_ids: tuple[UUID, ...] = ()
) -> PreviewDraft:
    return PreviewDraft.model_validate(
        {
            "preview_mode": mode,
            "prior_determination_set_ids": prior_ids,
            "jurisdiction_id": uuid4(),
            "occurred_on": date(2026, 1, 1),
            "fact_kind": "expected_withholding",
            "recognition_basis_code": "invoice_expectation",
            "transaction_side": "withholding",
            "base_amount": "100.00",
            "currency_code": "NGN",
            "minor_units": 2,
            "source_ref": "test:withholding",
            "source_version": "1",
            "evidence_ref": "test:preview",
        }
    )


def test_no_dependency_still_maps_losslessly_to_v2() -> None:
    command = _rule().tax_input()
    assert isinstance(command, TaxRuleInputV2)
    assert command.dependency_policy is None
    assert command.calculation_base_code == "source_amount"
    assert command.rate == Decimal("0.1")


@pytest.mark.parametrize(
    "selection", (DependencySelection.all_prior, DependencySelection.tax_code_ids)
)
def test_dependency_requires_complete_prior_signature_and_exact_code_selection(
    selection: DependencySelection,
) -> None:
    draft = _rule(selection=selection)
    with pytest.raises(ValueError, match="complete prior"):
        draft.tax_input()
    prior_jurisdiction, prior_code = uuid4(), uuid4()
    draft = draft.model_copy(
        update={
            "dependency_prior_jurisdiction_id": prior_jurisdiction,
            "dependency_prior_fact_kind": "invoice_tax",
            "dependency_prior_recognition_basis_code": "invoice_expectation",
            "dependency_prior_transaction_side": "output",
            "dependency_tax_code_ids": (prior_code,)
            if selection is DependencySelection.tax_code_ids
            else (),
        }
    )
    command = draft.tax_input()
    policy = command.dependency_policy
    assert policy is not None
    assert policy.prior_signature.jurisdiction_id == prior_jurisdiction
    assert policy.prior_signature.fact_kind == "invoice_tax"
    assert policy.prior_signature.recognition_basis_code == "invoice_expectation"
    assert policy.prior_signature.transaction_side == "output"
    assert policy.selection == selection.value
    assert policy.tax_code_ids == (
        (prior_code,) if selection is DependencySelection.tax_code_ids else ()
    )
    incompatible = draft.model_copy(
        update={
            "dependency_tax_code_ids": ()
            if selection is DependencySelection.tax_code_ids
            else (prior_code,)
        }
    )
    with pytest.raises(ValueError):
        incompatible.tax_input()


def test_bounded_picker_preserves_target_and_predecessor_for_dependency_authoring() -> (
    None
):
    target = PolicyIdentity(
        tax_code_id=uuid4(),
        jurisdiction_id=uuid4(),
        jurisdiction_code="JUR",
        tax_code="WHT",
    )
    prior_jurisdiction, first, second = uuid4(), uuid4(), uuid4()
    query = WorkbenchQuery(
        catalog="codes",
        dependency_target=target,
        dependency_predecessor_version=4,
        dependency_prior_jurisdiction_id=prior_jurisdiction,
        dependency_selected_codes=(first, second),
    )
    destination = query.dependency_author_url()
    assert destination is not None
    parsed = parse_qs(urlsplit(destination).query)
    assert urlsplit(destination).path.endswith("/author/supersede")
    assert parsed["tax_code_id"] == [str(target.tax_code_id)]
    assert parsed["predecessor_version"] == ["4"]
    assert parsed["dependency_selection"] == ["tax_code_ids"]
    assert str(first) in parsed["dependency_tax_code_ids"][0]
    assert str(second) in parsed["dependency_tax_code_ids"][0]
    all_prior = parse_qs(
        urlsplit(query.dependency_author_url(all_prior=True) or "").query
    )
    assert all_prior["dependency_tax_code_ids"] == ["[]"]
    assert all_prior["dependency_selection"] == ["all_prior"]


def test_dependent_preview_requires_actual_ids_without_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("V1 preview must never be a dependency fallback")

    monkeypatch.setattr(owner, "preview_tax_policy", forbidden)
    admission = PermissionAdmission(
        uuid4(), "system_user", frozenset({owner.READ_PERMISSION})
    )
    with pytest.raises(owner.TaxPolicyAuthoringError, match="actual sealed prior"):
        owner.preview_policy_details(
            Mock(spec=Session), admission=admission, draft=_preview()
        )

    def missing(*args: object, **kwargs: object) -> None:
        raise TaxNotFound("not available in tenant")

    monkeypatch.setattr(owner, "get_tax_determination_set_v2", missing)
    with pytest.raises(
        owner.TaxPolicyAuthoringError, match="exact original V2 result UUID"
    ):
        owner.preview_policy_details(
            Mock(spec=Session),
            admission=admission,
            draft=_preview(prior_ids=(uuid4(),)),
        )


def test_v1_preview_reports_persisted_dependency_with_actionable_evidence_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dependency(*args: object, **kwargs: object) -> None:
        raise TaxRuleViolation("dependency rule requires determine_tax_set_v2")

    monkeypatch.setattr(owner, "preview_tax_policy", dependency)
    admission = PermissionAdmission(
        uuid4(), "system_user", frozenset({owner.READ_PERMISSION})
    )
    with pytest.raises(owner.TaxPolicyAuthoringError, match="actual sealed prior V2"):
        owner.preview_policy_details(
            Mock(spec=Session), admission=admission, draft=_preview(mode="current")
        )
