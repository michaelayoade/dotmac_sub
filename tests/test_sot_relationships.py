from __future__ import annotations

import importlib

from app.services import sot_relationships


def test_domain_sot_relationships_cover_expected_domains():
    assert sot_relationships.domain_order() == [
        "customer_context",
        "financial_access",
        "network",
        "subscriber_sessions",
        "application_sessions",
        "secrets_credentials",
        "notifications_communications",
        "events_webhooks",
        "runtime_infrastructure",
        "observability",
        "support_operations",
        "provisioning_operations",
        "feature_control_plane",
        "authorization_control_plane",
        "scheduler_control_plane",
        "network_access_control_plane",
        "service_intent_control_plane",
        "integration_control_plane",
        "ui_list_projection",
        "ui_bulk_actions",
        "ui_semantic_presentation",
    ]


def test_domain_sot_relationships_encode_cross_domain_dependencies():
    assert sot_relationships.dependencies_for("network.outage_impact") == (
        "network.access_path",
    )
    assert sot_relationships.dependencies_for("network.outage_lifecycle") == (
        "network.outage_impact",
        "events.dispatcher",
    )
    assert sot_relationships.dependencies_for("network.device_groups") == (
        "network.identity",
    )
    assert sot_relationships.dependencies_for("network.routeros_sot") == (
        "network.identity",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("network.monitoring_inventory") == (
        "network.identity",
    )
    assert sot_relationships.dependencies_for("financial.dunning") == (
        "financial.access_resolution",
        "financial.ledger",
        "financial.payment_arrangements",
        "financial.billing_health",
        "access.subscription_lifecycle",
        "access.walled_garden_policy",
    )
    assert sot_relationships.dependencies_for("access.subscription_lifecycle") == (
        "events.dispatcher",
    )
    assert sot_relationships.dependencies_for("financial.access_resolution") == (
        "financial.billing_profile",
        "financial.prepaid_threshold",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.billing_scheduled") == (
        "financial.ledger",
        "financial.access_resolution",
        "financial.billing_health",
    )
    financial_services = sot_relationships.service_names_for_domain("financial_access")
    assert "financial.payment_arrangements" in financial_services
    assert "financial.billing_health" in financial_services
    assert sot_relationships.dependencies_for("financial.collections_scheduled") == (
        "financial.dunning",
        "financial.access_resolution",
        "financial.prepaid_enforcement",
    )
    assert sot_relationships.dependencies_for("financial.payment_webhooks") == (
        "financial.payment_provider_events",
    )
    assert sot_relationships.dependencies_for("financial.payment_provider_events") == (
        "financial.payments",
    )
    assert sot_relationships.dependencies_for("financial.payment_reconciliation") == (
        "financial.ledger",
        "financial.payment_provider_events",
    )
    assert sot_relationships.dependencies_for("customer.service_status") == (
        "financial.access_resolution",
        "customer.financial_position",
        "financial.grace_policy",
    )
    assert sot_relationships.dependencies_for("customer.usage_summary") == (
        "sessions.radius_reconciliation",
    )
    assert sot_relationships.dependencies_for("financial.prepaid_plan_change") == (
        "financial.account_adjustments",
        "financial.credit_notes",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.account_adjustments") == (
        "financial.ledger",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for(
        "financial.import_payment_batch_reversals"
    ) == (
        "financial.payments",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.addon_purchases") == (
        "financial.account_adjustments",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("ui.customer_list_projection") == (
        "ui.list_contracts",
    )
    customer_api_mapping = sot_relationships.owning_service_for(
        "legacy customer offset API compatibility mapping"
    )
    assert customer_api_mapping is not None
    assert customer_api_mapping.name == "ui.customer_list_projection"
    assert sot_relationships.dependencies_for("ui.subscriber_list_projection") == (
        "ui.list_contracts",
    )
    assert sot_relationships.dependencies_for("ui.invoice_list_projection") == (
        "ui.list_contracts",
        "financial.invoices",
    )
    assert sot_relationships.dependencies_for("ui.bulk_action_contracts") == (
        "ui.list_contracts",
    )
    assert sot_relationships.dependencies_for("ui.customer_bulk_action_projection") == (
        "ui.bulk_action_contracts",
        "ui.customer_list_projection",
    )
    assert sot_relationships.dependencies_for("ui.invoice_bulk_action_projection") == (
        "ui.bulk_action_contracts",
        "ui.invoice_list_projection",
        "financial.invoices",
    )
    assert sot_relationships.dependencies_for("ui.status_presentation") == (
        "financial.invoices",
        "financial.payments",
        "network.device_state",
        "network.connection_health",
        "network.outage_lifecycle",
        "support.ticket_lifecycle",
        "operations.work_order_status",
    )
    subscriber_api_mapping = sot_relationships.owning_service_for(
        "legacy subscriber offset API compatibility mapping"
    )
    assert subscriber_api_mapping is not None
    assert subscriber_api_mapping.name == "ui.subscriber_list_projection"
    assert sot_relationships.dependencies_for(
        "communications.notification_service"
    ) == ("communications.channel_policy", "communications.event_policy")
    assert sot_relationships.dependencies_for("communications.customer_read_state") == (
        "customer.identity_scope",
        "communications.customer_policy",
        "communications.notification_service",
    )
    assert sot_relationships.dependencies_for("secrets.rotation") == (
        "secrets.reference_store",
        "secrets.credential_integrity",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("secrets.credential_integrity") == (
        "secrets.access_credential_format",
        "secrets.credential_crypto",
        "observability.recording",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("secrets.credential_recovery") == (
        "secrets.credential_integrity",
        "network.identity",
        "network.radius_sessions",
        "access.radius_state",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("communications.team_inbox") == (
        "customer.identity_scope",
        "communications.channel_policy",
        "communications.notification_service",
    )
    assert sot_relationships.dependencies_for("sessions.enforcement") == (
        "financial.access_resolution",
        "sessions.radius_resolution",
    )
    assert sot_relationships.dependencies_for("sessions.radius_accounting_health") == (
        "control.domain_settings",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("runtime.infrastructure_polling") == (
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("operations.project_lifecycle") == (
        "events.dispatcher",
        "communications.staff_notifications",
    )
    assert sot_relationships.dependencies_for("operations.field_completion") == (
        "operations.work_orders",
        "operations.work_order_status",
        "control.domain_settings",
    )
    assert sot_relationships.dependencies_for("network.nas_lifecycle") == (
        "network.identity",
        "network.access_path",
        "network.radius_sessions",
        "network.nas_inventory",
        "service_intent.subscription_nas_assignment",
        "access.radius_state",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("network.nas_access_path_evidence") == (
        "network.radius_sessions",
        "network.nas_lifecycle",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("control.feature_registry") == (
        "control.module_manager",
        "control.domain_settings",
    )
    assert sot_relationships.dependencies_for("scheduler.registry") == (
        "control.feature_registry",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("access.radius_state") == (
        "access.control_resolution",
        "access.walled_garden_policy",
    )
    assert sot_relationships.dependencies_for("sessions.radius_resolution") == (
        "sessions.radius_reconciliation",
        "network.identity",
    )
    assert sot_relationships.dependencies_for(
        "service_intent.catalog_billing_governance"
    ) == (
        "service_intent.catalog_validation",
        "auth.permission_gate",
        "observability.recording",
    )


def test_domain_sot_relationships_resolve_owning_service_by_concern():
    service = sot_relationships.owning_service_for("RADIUS access decision")

    assert service is not None
    assert service.name == "financial.access_resolution"
    assert service.module == "app.services.access_resolution"

    presentation_service = sot_relationships.owning_service_for(
        "account status labels, semantic tones, and icon keys"
    )

    assert presentation_service is not None
    assert presentation_service.name == "ui.status_presentation"
    assert presentation_service.module == "app.services.status_presentation"

    work_order_status = sot_relationships.owning_service_for(
        "persisted work-order status vocabulary"
    )
    assert work_order_status is not None
    assert work_order_status.name == "operations.work_order_status"

    work_order_presentation = sot_relationships.owning_service_for(
        "field work-order status labels, semantic tones, and icon keys"
    )
    assert work_order_presentation is not None
    assert work_order_presentation.name == "ui.status_presentation"

    ticket_lifecycle = sot_relationships.owning_service_for(
        "guarded ticket status transitions"
    )
    assert ticket_lifecycle is not None
    assert ticket_lifecycle.name == "support.ticket_lifecycle"
    assert ticket_lifecycle.module == "app.services.support"

    ticket_presentation = sot_relationships.owning_service_for(
        "support-ticket status labels, semantic tones, and icon keys"
    )
    assert ticket_presentation is not None
    assert ticket_presentation.name == "ui.status_presentation"

    invoice_presentation = sot_relationships.owning_service_for(
        "invoice status labels, semantic tones, and icon keys"
    )
    assert invoice_presentation is not None
    assert invoice_presentation.name == "ui.status_presentation"

    invoice_lifecycle = sot_relationships.owning_service_for(
        "invoice status transitions"
    )
    assert invoice_lifecycle is not None
    assert invoice_lifecycle.name == "financial.invoices"

    automation_invoice_owner = sot_relationships.owning_service_for(
        "automation invoice creation and draft issuance"
    )
    assert automation_invoice_owner is not None
    assert automation_invoice_owner.name == "financial.invoices"

    overdue_invoice_owner = sot_relationships.owning_service_for(
        "overdue invoice state and observation event"
    )
    assert overdue_invoice_owner is not None
    assert overdue_invoice_owner.name == "financial.invoices"

    payment_presentation = sot_relationships.owning_service_for(
        "payment status labels, semantic tones, and icon keys"
    )
    assert payment_presentation is not None
    assert payment_presentation.name == "ui.status_presentation"

    payment_lifecycle = sot_relationships.owning_service_for(
        "payment document lifecycle"
    )
    assert payment_lifecycle is not None
    assert payment_lifecycle.name == "financial.payments"

    settlement_owner = sot_relationships.owning_service_for(
        "confirmed payment settlement preview and evidence"
    )
    assert settlement_owner is not None
    assert settlement_owner.name == "financial.payments"

    consolidated_settlement_owner = sot_relationships.owning_service_for(
        "consolidated payment settlement preview and confirmation"
    )
    assert consolidated_settlement_owner is not None
    assert consolidated_settlement_owner.name == "financial.consolidated_payments"

    consolidated_ledger_owner = sot_relationships.owning_service_for(
        "exact consolidated-credit ledger links"
    )
    assert consolidated_ledger_owner is not None
    assert consolidated_ledger_owner.name == "financial.consolidated_payments"

    allocation_owner = sot_relationships.owning_service_for(
        "settled account-credit allocation preview and confirmation"
    )
    assert allocation_owner is not None
    assert allocation_owner.name == "financial.payments"

    refund_owner = sot_relationships.owning_service_for(
        "payment refund confirmation and exact ledger evidence"
    )
    assert refund_owner is not None
    assert refund_owner.name == "financial.payments"

    reversal_owner = sot_relationships.owning_service_for(
        "payment reversal confirmation and exact ledger evidence"
    )
    assert reversal_owner is not None
    assert reversal_owner.name == "financial.payments"

    provider_reversal_owner = sot_relationships.owning_service_for(
        "normalized provider reversal evidence"
    )
    assert provider_reversal_owner is not None
    assert provider_reversal_owner.name == "financial.payments"

    batch_reversal_owner = sot_relationships.owning_service_for(
        "exact import-row-to-settlement-to-reversal ledger links"
    )
    assert batch_reversal_owner is not None
    assert batch_reversal_owner.name == "financial.import_payment_batch_reversals"

    account_adjustment_owner = sot_relationships.owning_service_for(
        "exact account-adjustment ledger links"
    )
    assert account_adjustment_owner is not None
    assert account_adjustment_owner.name == "financial.account_adjustments"

    addon_purchase_owner = sot_relationships.owning_service_for(
        "exact add-on entitlement-to-adjustment link"
    )
    assert addon_purchase_owner is not None
    assert addon_purchase_owner.name == "financial.addon_purchases"

    semantic_palette = sot_relationships.owning_service_for(
        "brand primary, secondary, and semantic UI color roles"
    )
    assert semantic_palette is not None
    assert semantic_palette.name == "customer.branding"

    outage_lifecycle = sot_relationships.owning_service_for(
        "persisted outage incident status vocabulary"
    )
    assert outage_lifecycle is not None
    assert outage_lifecycle.name == "network.outage_lifecycle"

    outage_presentation = sot_relationships.owning_service_for(
        "outage incident status labels, semantic tones, and icon keys"
    )
    assert outage_presentation is not None
    assert outage_presentation.name == "ui.status_presentation"

    device_state = sot_relationships.owning_service_for(
        "device operational status vocabulary"
    )
    assert device_state is not None
    assert device_state.name == "network.device_state"

    device_presentation = sot_relationships.owning_service_for(
        "device operational status labels, semantic tones, and icon keys"
    )
    assert device_presentation is not None
    assert device_presentation.name == "ui.status_presentation"

    connection_health = sot_relationships.owning_service_for(
        "customer-safe connection health vocabulary"
    )
    assert connection_health is not None
    assert connection_health.name == "network.connection_health"

    connection_presentation = sot_relationships.owning_service_for(
        "customer connection health labels, semantic tones, and icon keys"
    )
    assert connection_presentation is not None
    assert connection_presentation.name == "ui.status_presentation"

    control_service = sot_relationships.owning_service_for(
        "module/feature/safety control resolution"
    )

    assert control_service is not None
    assert control_service.name == "control.feature_registry"

    project_service = sot_relationships.owning_service_for(
        "native project field and status mutations"
    )

    assert project_service is not None
    assert project_service.name == "operations.project_lifecycle"
    assert project_service.module == "app.services.projects"

    completion_service = sot_relationships.owning_service_for(
        "field completion evidence requirements"
    )

    assert completion_service is not None
    assert completion_service.name == "operations.field_completion"
    assert completion_service.module == "app.services.field.transitions"

    action_service = sot_relationships.owning_service_for(
        "payment-restores-service claims"
    )

    assert action_service is not None
    assert action_service.name == "customer.service_status"
    assert action_service.module == "app.services.service_status"

    usage_service = sot_relationships.owning_service_for(
        "customer usage headline totals"
    )

    assert usage_service is not None
    assert usage_service.name == "customer.usage_summary"
    assert usage_service.module == "app.services.usage_summary"

    read_state_service = sot_relationships.owning_service_for(
        "customer notification read/unread state"
    )

    assert read_state_service is not None
    assert read_state_service.name == "communications.customer_read_state"
    assert read_state_service.module == "app.services.customer_portal_notifications"

    team_inbox_service = sot_relationships.owning_service_for(
        "admin inbox mutation transactions"
    )

    assert team_inbox_service is not None
    assert team_inbox_service.name == "communications.team_inbox"
    assert team_inbox_service.module == "app.services.team_inbox_commands"


def test_domain_sot_relationship_modules_are_importable():
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            importlib.import_module(service.module)
