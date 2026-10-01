"""The payment-template operator adapter stays explicit and evidence-bound."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.auth_dependencies import permission_requirement
from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id
from app.services.payment_template_adoption import (
    AdoptionItem,
    AdoptionResult,
    ParityItem,
    ParityReport,
    ParityStatus,
)
from app.web.admin import notifications as routes


def _report(receipt_id, paid_id, status=ParityStatus.studio_missing):
    return ParityReport(
        tenant_id=operator_tenant_id(),
        items=(
            ParityItem("payment_received", receipt_id, None, status, 2, True, {}),
            ParityItem("invoice_paid", paid_id, None, status, 2, False, {}),
        ),
    )


def _command(receipt_id, paid_id):
    return routes.PaymentTemplateAdoptionRequest(
        confirm="ADOPT_PAYMENT_EMAIL_TEMPLATES",
        payment_received_legacy_id=receipt_id,
        invoice_paid_legacy_id=paid_id,
    )


def _request(auth=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            auth=auth or {"principal_type": "system_user", "principal_id": str(uuid4())}
        )
    )


def _body(response):
    return json.loads(response.body)


def test_parity_endpoint_is_read_only_and_uses_owner_samples(monkeypatch):
    receipt_id, paid_id = uuid4(), uuid4()
    calls = []

    def report(_db):
        calls.append("report")
        return _report(receipt_id, paid_id)

    monkeypatch.setattr(
        routes.payment_template_adoption, "payment_email_parity_report", report
    )
    monkeypatch.setattr(
        routes.payment_template_adoption,
        "adopt_payment_email_templates",
        lambda *_args, **_kwargs: pytest.fail("read endpoint invoked adoption"),
    )
    response = routes.payment_email_adoption_parity(db=object())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert _body(response)["items"][0]["legacy_template_id"] == str(receipt_id)
    assert calls == ["report"]
    samples = routes.payment_template_adoption.REPRESENTATIVE_PAYMENT_EMAIL_CONTEXTS
    assert len(samples["payment_received"]) == 2
    assert len(samples["invoice_paid"]) == 2
    assert "receipt_url" in samples["payment_received"][0]
    assert "receipt_url" not in samples["invoice_paid"][0]


def test_post_releases_read_transaction_then_invokes_one_scoped_owner_command(
    monkeypatch,
):
    receipt_id, paid_id = uuid4(), uuid4()
    studio_receipt, studio_paid = uuid4(), uuid4()
    after = _report(receipt_id, paid_id, ParityStatus.match)
    steps = []

    def report(_db):
        steps.append("report")
        return after

    def adopt(_db, *, context, reviewed):
        steps.append("adopt")
        assert context.actor.startswith("system_user:")
        assert context.scope == str(operator_tenant_id())
        assert str(receipt_id) in context.idempotency_key
        assert reviewed.payment_received_legacy_id == receipt_id
        assert reviewed.invoice_paid_legacy_id == paid_id
        return AdoptionResult(
            tenant_id=operator_tenant_id(),
            items=(
                AdoptionItem("payment_received", receipt_id, studio_receipt, 1, True),
                AdoptionItem("invoice_paid", paid_id, studio_paid, 1, True),
            ),
        )

    monkeypatch.setattr(
        routes.payment_template_adoption, "payment_email_parity_report", report
    )
    monkeypatch.setattr(
        routes.payment_template_adoption, "adopt_payment_email_templates", adopt
    )
    monkeypatch.setattr(
        routes.db_session_adapter,
        "release_read_transaction",
        lambda _db: steps.append("release"),
    )
    response = routes.payment_email_adoption_run(
        _request(), _command(receipt_id, paid_id), db=object()
    )
    assert steps == ["release", "adopt", "report"]
    assert response.status_code == 200
    assert _body(response)["adoption"]["items"][0]["studio_template_id"] == str(
        studio_receipt
    )
    assert _body(response)["parity"]["items"][0]["status"] == "match"


def test_post_maps_owner_conflict_without_customer_content(monkeypatch):
    receipt_id, paid_id = uuid4(), uuid4()
    monkeypatch.setattr(
        routes.payment_template_adoption,
        "payment_email_parity_report",
        lambda *_args, **_kwargs: pytest.fail("conflict reran parity in the adapter"),
    )

    def fail(_db, *, context, reviewed):
        assert reviewed.payment_received_legacy_id == receipt_id
        assert reviewed.invoice_paid_legacy_id == paid_id
        raise DomainError(
            code="payment_template_adoption.studio_conflict",
            message="Existing Studio template differs.",
        )

    monkeypatch.setattr(
        routes.payment_template_adoption, "adopt_payment_email_templates", fail
    )
    monkeypatch.setattr(
        routes.db_session_adapter, "release_read_transaction", lambda _db: None
    )
    response = routes.payment_email_adoption_run(
        _request(), _command(receipt_id, paid_id), db=object()
    )
    assert response.status_code == 409
    assert _body(response) == {
        "error": "Existing Studio template differs.",
        "code": "payment_template_adoption.studio_conflict",
    }


def test_post_requires_staff_identity_and_exact_confirmation(monkeypatch):
    receipt_id, paid_id = uuid4(), uuid4()
    monkeypatch.setattr(
        routes.payment_template_adoption,
        "adopt_payment_email_templates",
        lambda *_args, **_kwargs: pytest.fail("non-staff reached the owner"),
    )
    response = routes.payment_email_adoption_run(
        _request({"principal_type": "subscriber", "principal_id": str(uuid4())}),
        _command(receipt_id, paid_id),
        db=object(),
    )
    assert response.status_code == 403
    with pytest.raises(ValidationError):
        routes.PaymentTemplateAdoptionRequest(
            confirm="yes",
            payment_received_legacy_id=receipt_id,
            invoice_paid_legacy_id=paid_id,
        )


def test_routes_require_notification_permissions_and_admin_csrf():
    from app.web import admin as admin_router_module
    from app.web.auth.dependencies import require_admin_web_auth

    assert any(
        dependency.dependency is require_admin_web_auth
        for dependency in admin_router_module.router.dependencies
    )
    routes_by_path = {
        (route.path, next(iter(route.methods))): route for route in routes.router.routes
    }
    for path, method, permission in (
        ("/notifications/payment-email-adoption/parity", "GET", "notification:read"),
        ("/notifications/payment-email-adoption", "POST", "notification:write"),
    ):
        route = routes_by_path[(path, method)]
        requirements = [
            permission_requirement(dependency.dependency)
            for dependency in route.dependencies
        ]
        assert any(
            requirement is not None and permission in requirement.any_of_for(method)
            for requirement in requirements
        )
    from app.main import _CSRF_PROTECTED_PATHS

    assert "/admin/" in _CSRF_PROTECTED_PATHS
