from app.services.control_relationships import (
    HANDLER_CONTROLS,
    event_execution_plan,
    validate_event_execution_policy,
)
from app.services.events.types import EventType


def _declared_handlers():
    return [type(name, (), {})() for name in HANDLER_CONTROLS]


def test_every_registered_handler_has_a_valid_scope_and_execution_contract():
    validate_event_execution_policy(_declared_handlers())


def test_retired_crm_writer_is_not_in_event_topology():
    assert "CrmSyncHandler" not in HANDLER_CONTROLS


def test_money_events_do_not_suppress_independent_customer_or_external_outputs():
    handlers = _declared_handlers()
    for event_type in (EventType.payment_received, EventType.invoice_overdue):
        plan = event_execution_plan(event_type.value, handlers)
        assert plan
        assert all(not step.dependencies for step in plan)


def test_activation_enforcement_cannot_run_before_provisioning():
    plan = event_execution_plan(
        EventType.subscription_activated.value, _declared_handlers()
    )
    enforcement = next(
        step for step in plan if step.handler_name == "EnforcementHandler"
    )
    assert enforcement.dependencies == ("ProvisioningHandler",)
