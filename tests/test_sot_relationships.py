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
        "network.device_state",
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
    assert sot_relationships.dependencies_for(
        "communications.notification_service"
    ) == ("communications.channel_policy", "communications.event_policy")
    assert sot_relationships.dependencies_for("sessions.enforcement") == (
        "financial.access_resolution",
        "sessions.radius_resolution",
    )
    assert sot_relationships.dependencies_for("runtime.infrastructure_polling") == (
        "runtime.db_sessions",
        "network.device_state",
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
    assert sot_relationships.dependencies_for("service_intent.catalog_to_network") == (
        "service_intent.catalog_policy",
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


def test_domain_sot_relationship_modules_are_importable():
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            importlib.import_module(service.module)
