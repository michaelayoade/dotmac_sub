"""Focused public contracts; executable qualification belongs to hosted CI."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.action_forms import ActionFormSubmission
from app.services.permission_admission import PermissionAdmission
from app.services.tax_policy_authoring import (
    READ_PERMISSION,
    TaxPolicyAuthoringError,
    admit,
)
from app.services.tax_policy_authoring_contracts import Operation, exact_money
from app.services.tax_policy_authoring_workbench import WorkbenchQuery, authoring_form


def test_money_refuses_silent_currency_rounding_and_special_values() -> None:
    for amount in ("1.001", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            exact_money(Decimal(amount), "NGN", 2)
    assert exact_money(Decimal("0.00"), "NGN", 2).amount == Decimal("0.00")


def test_workbench_is_bounded_and_preserves_owner_cursor_scope() -> None:
    with pytest.raises(ValidationError):
        WorkbenchQuery(page_size=101)
    query = WorkbenchQuery(
        catalog="codes", jurisdiction_id=uuid4(), tax_code_id=uuid4(), cursor="old"
    )
    url = query.url(cursor="owner cursor+/", history_cursor="history")
    assert "owner+cursor%2B%2F" in url
    assert str(query.tax_code_id) in url
    assert "old" not in url


def test_staff_admission_refuses_subscriber_and_wrong_permission() -> None:
    for admission in (
        PermissionAdmission(uuid4(), "subscriber", frozenset({READ_PERMISSION})),
        PermissionAdmission(uuid4(), "system_user", frozenset()),
    ):
        with pytest.raises(TaxPolicyAuthoringError, match="authorized staff"):
            admit(admission=admission, permission=READ_PERMISSION)


def test_forms_have_no_inferred_policy_rate_and_bind_failed_input() -> None:
    form = authoring_form(operation=Operation.supersede)
    assert form.confirmation is not None
    assert form.field("rate").value == ""
    assert form.field("inclusive").value == ""
    assert form.field("predecessor_rule_id").required
    assert form.field("evidence_ref").required
    bound = authoring_form(
        operation=Operation.supersede,
        submission=ActionFormSubmission.from_mapping(
            form.key,
            {"predecessor_version": "bad"},
            field_errors={"predecessor_version": "Use an integer"},
        ),
    )
    assert bound.field("predecessor_version").value == "bad"
    assert bound.field("predecessor_version").error == "Use an integer"
    preview = authoring_form(operation=Operation.preview)
    assert preview.confirmation is None
    assert "persisted" in preview.title


@pytest.mark.parametrize(
    ("currency_code", "minor_units", "matches"),
    (("NGN", 2, True), ("USD", 2, False), ("NGN", 3, False)),
)
def test_rule_currency_checks_public_catalog_units(
    currency_code: str, minor_units: int, matches: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date
    from unittest.mock import Mock

    from dotmac_kernel.money import Currency
    from dotmac_tax import TaxCodeV1, TaxJurisdictionV1
    from sqlalchemy.orm import Session

    from app.services import tax_policy_authoring as owner
    from app.services.operator_tenant import OPERATOR_TENANT_ID
    from app.services.tax_policy_authoring_contracts import RuleDraft

    jurisdiction_id, code_id, authority_id = uuid4(), uuid4(), uuid4()
    code = TaxCodeV1(
        OPERATOR_TENANT_ID,
        code_id,
        jurisdiction_id,
        "CODE",
        "Code",
        "test",
        None,
        "active",
    )
    jurisdiction = TaxJurisdictionV1(
        OPERATOR_TENANT_ID,
        jurisdiction_id,
        authority_id,
        "JUR",
        "Jurisdiction",
        "NG",
        None,
        Currency("NGN", 2),
        "active",
    )
    monkeypatch.setattr(owner, "get_tax_code", lambda *args, **kwargs: code)
    monkeypatch.setattr(
        owner, "get_tax_jurisdiction", lambda *args, **kwargs: jurisdiction
    )
    draft = RuleDraft(
        tax_code_id=code_id,
        jurisdiction_id=jurisdiction_id,
        tax_code="CODE",
        jurisdiction_code="JUR",
        version=1,
        effective_from=date(2026, 1, 1),
        priority=100,
        fact_kind="test_invoice",
        recognition_basis_code="invoice_expectation",
        transaction_side="output",
        calculation_method="percentage",
        rate=Decimal("0.1"),
        currency_code=currency_code,
        minor_units=minor_units,
        inclusive=False,
        recoverable_rate=Decimal("0"),
        treatment_code="standard_rated",
        calculation_sequence=100,
        calculation_base_code="source_amount",
        reason="Approved",
        evidence_ref="decision:1",
        source_ref="policy:1",
        source_version="1",
    )
    if matches:
        owner.validate_rule_currency(db=Mock(spec=Session), draft=draft)
    else:
        with pytest.raises(TaxPolicyAuthoringError, match="currency"):
            owner.validate_rule_currency(db=Mock(spec=Session), draft=draft)


def test_publication_destination_retains_exact_review_scope() -> None:
    from urllib.parse import parse_qs, urlsplit

    from app.services.tax_policy_authoring_contracts import AuthoringOutcome
    from app.services.tax_policy_authoring_workbench import publication_destination

    entity_id, jurisdiction_id, code_id = uuid4(), uuid4(), uuid4()
    for operation in (Operation.code, Operation.initial_rule, Operation.supersede):
        destination = publication_destination(
            outcome=AuthoringOutcome(
                entity_id, operation, jurisdiction_id, code_id, "JUR", "CODE"
            )
        )
        query = parse_qs(urlsplit(destination).query)
        assert query["tax_code_id"] == [str(code_id)]
        assert query["jurisdiction_id"] == [str(jurisdiction_id)]
        assert query["jurisdiction_code"] == ["JUR"]
        assert query["tax_code"] == ["CODE"]
        guided = WorkbenchQuery.model_validate(
            {key: values[0] for key, values in query.items()}
        )
        seed_query = parse_qs(urlsplit(guided.author_url(predecessor_version=1)).query)
        assert seed_query["jurisdiction_code"] == ["JUR"]
        assert seed_query["tax_code"] == ["CODE"]
        assert seed_query["jurisdiction_id"] == [str(jurisdiction_id)]
        assert "success" not in query
