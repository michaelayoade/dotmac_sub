"""Transport guards and exact typed form parsing, without a database surrogate."""

from pathlib import Path

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.datastructures import FormData

from app.services.tax_policy_authoring_contracts import AuthorityDraft, Operation
from app.web.admin.billing_tax_policy import _parse, router


def test_authority_parser_rejects_actor_injection_and_duplicate_fields() -> None:
    for form in (
        FormData([("actor_id", "forged")]),
        FormData([("code", "a"), ("code", "b")]),
    ):
        with pytest.raises(ValueError):
            _parse(operation=Operation.authority, form=form)


def test_parser_preserves_typed_provenance_and_rejects_missing_evidence() -> None:
    values = {
        "code": "AUTH",
        "name": "Authority",
        "reason": "Approved policy",
        "evidence_ref": "decision:1",
        "source_ref": "policy:1",
        "source_version": "1",
    }
    draft = _parse(operation=Operation.authority, form=FormData(values))
    assert isinstance(draft, AuthorityDraft)
    assert draft.evidence_ref == "decision:1"
    del values["evidence_ref"]
    with pytest.raises(ValidationError):
        _parse(operation=Operation.authority, form=FormData(values))


def test_every_route_requires_canonical_permission_admission() -> None:
    def has_admission(dependant: Dependant) -> bool:
        return any(
            getattr(dependency.call, "__name__", "") == "admission"
            or has_admission(dependency)
            for dependency in dependant.dependencies
        )

    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert has_admission(route.dependant)


def test_shared_form_renderer_carries_csrf_and_explicit_confirmation() -> None:
    template = Path("templates/admin/billing/tax_policy/form.html").read_text()
    renderer = Path("templates/components/forms/action_form.html").read_text()
    assert "action_form(action)" in template
    assert "components/forms/csrf_input.html" in renderer
    assert 'name="confirmed"' in renderer


def test_canonical_confirmation_and_staff_actor_reach_typed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock
    from uuid import uuid4

    from sqlalchemy.orm import Session
    from starlette.requests import Request

    from app.services.permission_admission import PermissionAdmission
    from app.services.tax_policy_authoring import WRITE_PERMISSION, AuthorTaxPolicy
    from app.services.tax_policy_authoring_contracts import AuthoringOutcome
    from app.web.admin import billing_tax_policy as adapter

    captured: list[AuthorTaxPolicy] = []

    def author(db: Session, *, command: AuthorTaxPolicy) -> AuthoringOutcome:
        captured.append(command)
        return AuthoringOutcome(entity_id=uuid4(), operation=Operation.authority)

    monkeypatch.setattr(adapter, "author_tax_policy", author)
    monkeypatch.setattr(adapter, "finish_read_transaction", lambda db: None)
    actor_id = uuid4()
    response = adapter.policy_author(
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/admin/billing/tax-policy/authority",
                "headers": [],
            }
        ),
        operation=Operation.authority,
        form=FormData(
            {
                "code": "AUTH",
                "name": "Authority",
                "reason": "Approved",
                "source_ref": "policy:1",
                "source_version": "1",
                "evidence_ref": "decision:1",
                "confirmed": "yes",
            }
        ),
        admission=PermissionAdmission(
            actor_id, "system_user", frozenset({WRITE_PERMISSION})
        ),
        db=Mock(spec=Session),
    )
    assert response.status_code == 303
    assert "catalog=jurisdictions" in response.headers["location"]
    assert "authority_id=" in response.headers["location"]
    assert captured[0].confirmed is True
    assert captured[0].context.actor == f"system_user:{actor_id}"
    assert captured[0].admission.principal_id == actor_id


def test_canonical_csrf_middleware_refuses_publication_without_token() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.main import csrf_middleware

    application = FastAPI()
    application.middleware("http")(csrf_middleware)
    application.include_router(router, prefix="/admin")
    with TestClient(application) as client:
        response = client.post(
            "/admin/billing/tax-policy/authority", data={"code": "AUTH"}
        )
    assert response.status_code == 403


def test_nested_staff_guards_refuse_subscriber_admission() -> None:
    from uuid import uuid4

    from fastapi import HTTPException

    from app.services.permission_admission import PermissionAdmission
    from app.services.tax_policy_authoring import READ_PERMISSION, WRITE_PERMISSION
    from app.web.admin.billing_tax_policy import _staff_read, _staff_write

    for guard, permission in (
        (_staff_read, READ_PERMISSION),
        (_staff_write, WRITE_PERMISSION),
    ):
        with pytest.raises(HTTPException) as refusal:
            guard(
                admission=PermissionAdmission(
                    uuid4(), "subscriber", frozenset({permission})
                )
            )
        assert refusal.value.status_code == 403


def test_preview_parser_accepts_result_uuids_but_refuses_entered_fingerprints() -> None:
    from uuid import uuid4

    from app.services.tax_policy_authoring_contracts import PreviewDraft

    identifier = uuid4()
    values = {
        "preview_mode": "dependency",
        "prior_determination_set_ids": f'["{identifier}"]',
        "jurisdiction_id": str(uuid4()),
        "occurred_on": "2026-01-01",
        "fact_kind": "expected_withholding",
        "recognition_basis_code": "invoice_expectation",
        "transaction_side": "withholding",
        "base_amount": "100.00",
        "currency_code": "NGN",
        "minor_units": "2",
        "source_ref": "test:source",
        "source_version": "1",
        "evidence_ref": "test:evidence",
    }
    draft = _parse(operation=Operation.preview, form=FormData(values))
    assert isinstance(draft, PreviewDraft)
    assert draft.prior_determination_set_ids == (identifier,)
    values["prior_result_fingerprint"] = "invented"
    with pytest.raises(ValueError, match="Unknown"):
        _parse(operation=Operation.preview, form=FormData(values))
