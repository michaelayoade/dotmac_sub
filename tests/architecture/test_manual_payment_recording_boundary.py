from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship


def _source(path: str) -> str:
    return Path(path).read_text()


def test_manual_payment_recording_is_a_contracted_coordinator():
    service = service_relationship("financial.manual_payment_recording")

    assert service.module == "app.services.manual_payment_recording"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    concern = next(
        item
        for item in service.contract.concerns
        if item.name == "locked administrative manual-payment confirmation"
    )
    assert concern.role is OwnerRole.APPLICATION_COORDINATOR
    assert "financial.payments" in service.depends_on
    assert "financial.payment_proofs" in service.depends_on


def test_admin_and_api_adapters_use_duplicate_control_coordinator():
    api = _source("app/api/billing.py")
    web = _source("app/services/web_billing_payments.py")
    owner = _source("app/services/manual_payment_recording.py")

    assert "confirm_manual_payment_recording" in api
    assert "confirm_manual_payment_recording" in web
    assert "payments.confirm_creation(" not in api
    assert "payments.confirm_creation(" not in web
    assert "Payments.stage_confirm_creation(" in owner
    assert "execute_owner_command(" in owner


def test_duplicate_control_is_visible_and_server_enforced():
    form = _source("templates/admin/billing/payment_form.html")
    confirmation = _source("templates/admin/billing/payment_create_confirm.html")
    route = _source("app/web/admin/billing_payments.py")

    assert 'name="reference"' in form
    assert 'name="control_fingerprint"' in confirmation
    assert 'name="duplicate_risk_acknowledged"' in confirmation
    assert "Review submitted proof" in confirmation
    assert "duplicate_risk_acknowledged: bool = Form(False)" in route
