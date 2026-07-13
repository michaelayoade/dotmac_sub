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
        "provisioning_operations",
        "feature_control_plane",
        "authorization_control_plane",
        "scheduler_control_plane",
        "network_access_control_plane",
        "service_intent_control_plane",
        "integration_control_plane",
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
    assert sot_relationships.dependencies_for("network.monitoring_inventory") == (
        "network.identity",
    )
    assert sot_relationships.dependencies_for("financial.dunning") == (
        "financial.access_resolution",
        "financial.ledger",
    )
    assert sot_relationships.dependencies_for("financial.access_resolution") == (
        "financial.billing_profile",
        "financial.prepaid_threshold",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.billing_scheduled") == (
        "financial.ledger",
        "financial.access_resolution",
    )
    assert sot_relationships.dependencies_for("financial.collections_scheduled") == (
        "financial.dunning",
        "financial.access_resolution",
        "financial.prepaid_enforcement",
    )
    assert sot_relationships.dependencies_for("financial.payment_webhooks") == (
        "financial.payment_provider_events",
    )
    assert sot_relationships.dependencies_for("financial.vas_wallet") == (
        "financial.payments",
    )
    assert sot_relationships.dependencies_for("financial.payment_provider_events") == (
        "financial.payments",
    )
    assert sot_relationships.dependencies_for("financial.payment_reconciliation") == (
        "financial.ledger",
        "financial.payment_provider_events",
    )
    assert sot_relationships.dependencies_for("financial.vas_operations") == (
        "control.domain_settings",
        "financial.vas_refunds",
    )
    assert sot_relationships.dependencies_for("financial.vas_refunds") == (
        "control.domain_settings",
    )
    assert sot_relationships.dependencies_for("customer.service_status") == (
        "financial.access_resolution",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("customer.usage_summary") == (
        "sessions.radius_live_view",
    )
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
        "access.event_policy",
    )
    assert sot_relationships.dependencies_for("sessions.radius_resolution") == (
        "sessions.radius_reconciliation",
        "network.identity",
    )


def test_domain_sot_relationships_resolve_owning_service_by_concern():
    service = sot_relationships.owning_service_for("RADIUS access decision")

    assert service is not None
    assert service.name == "financial.access_resolution"
    assert service.module == "app.services.access_resolution"

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

    vas_service = sot_relationships.owning_service_for(
        "VAS refund-to-source eligibility"
    )

    assert vas_service is not None
    assert vas_service.name == "financial.vas_refunds"
    assert vas_service.module == "app.services.vas_refunds"

    refund_reconciliation = sot_relationships.owning_service_for(
        "VAS refund provider reconciliation"
    )

    assert refund_reconciliation is vas_service


def test_domain_sot_relationship_modules_are_importable():
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            importlib.import_module(service.module)
