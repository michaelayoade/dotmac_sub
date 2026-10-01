"""The explicit adoption coordinator is dormant and contract bound."""

from pathlib import Path

from dotmac_template_studio import registered_contexts

from app.services import payment_template_adoption  # noqa: F401 - registers contexts
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_registry.registry import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_adoption_owner_has_complete_shadow_contract() -> None:
    owner = service_relationship("communications.payment_template_adoption")
    assert owner.contract is not None
    assert not contract_validation_errors(
        owner, service_names={service.name for service in all_services()}
    )
    assert owner.contract.concerns[0].role is OwnerRole.APPLICATION_COORDINATOR
    assert owner.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert owner.contract.migration.state is AuthorityMigrationState.SHADOWING
    assert owner.contract.events is None


def test_adoption_has_no_handler_or_task_caller() -> None:
    handler = (ROOT / "app/services/events/handlers/notification.py").read_text()
    tasks = (ROOT / "app/tasks/__init__.py").read_text()
    schedule = (ROOT / "app/services/scheduler_config.py").read_text()
    assert "adopt_payment_email_templates" not in handler
    assert "adopt_payment_email_templates" not in tasks
    assert "adopt_payment_email_templates" not in schedule


def test_adoption_does_not_emit_a_dispatchable_domain_event() -> None:
    source = (ROOT / "app/services/payment_template_adoption.py").read_text()
    event_types = (ROOT / "app/services/events/types.py").read_text()
    assert "emit_event(" not in source
    assert "payment_template.adopted" not in event_types


def test_payment_render_contexts_do_not_cross_event_vocabulary() -> None:
    contexts = {context.name: context.variables for context in registered_contexts()}
    assert contexts["sub_payment_receipt_email"] == frozenset(
        {"subscriber_name", "amount", "portal_url", "receipt_number", "receipt_url"}
    )
    assert contexts["sub_invoice_paid_email"] == frozenset(
        {"subscriber_name", "amount", "invoice_number", "portal_url", "invoice_url"}
    )
