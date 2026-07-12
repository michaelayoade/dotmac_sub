"""Machine-readable exclusivity, precedence, chain, and fanout registry."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain


class RelationshipMode(str, enum.Enum):
    exclusive = "exclusive"
    precedence = "precedence"
    chain = "chain"
    fanout = "fanout"
    competing = "competing"
    incompatible = "incompatible"


class HandlerStage(enum.IntEnum):
    state = 10
    communication = 20
    external = 30


@dataclass(frozen=True)
class SettingRef:
    domain: SettingDomain
    key: str

    @property
    def locator(self) -> str:
        return f"{self.domain.value}.{self.key}"


@dataclass(frozen=True)
class ControlRelationship:
    name: str
    mode: RelationshipMode
    members: tuple[str, ...]
    rule: str


@dataclass(frozen=True)
class HandlerControl:
    handler_name: str
    stage: HandlerStage
    order: int
    capabilities: tuple[str, ...]
    relationship: RelationshipMode = RelationshipMode.fanout


@dataclass(frozen=True)
class ControlFinding:
    code: str
    severity: str
    message: str
    members: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ControlRelationshipError(ValueError):
    pass


CONTROL_RELATIONSHIPS: tuple[ControlRelationship, ...] = (
    ControlRelationship(
        name="branding_resolution",
        mode=RelationshipMode.precedence,
        members=(
            "brand.organization_profile",
            "brand.reseller_profile",
            "brand.platform_profile",
            "legacy.domain_settings",
            "deployment.brand_config",
        ),
        rule="First non-empty field wins in declared tenant-to-platform order.",
    ),
    ControlRelationship(
        name="payment_gateway_failover",
        mode=RelationshipMode.exclusive,
        members=(
            "billing.payment_gateway_primary_provider",
            "billing.payment_gateway_secondary_provider",
        ),
        rule="Primary and secondary providers must differ while failover is enabled.",
    ),
    ControlRelationship(
        name="team_email_sender_resolution",
        mode=RelationshipMode.precedence,
        members=(
            "team.sender_profile",
            "category.sender_profile",
            "default.sender_profile",
            "deployment.smtp_identity",
        ),
        rule="Team-specific outbound identity wins without changing tenant branding.",
    ),
    ControlRelationship(
        name="whatsapp_provider_selection",
        mode=RelationshipMode.exclusive,
        members=("meta_cloud_api", "twilio", "messagebird"),
        rule="The whatsapp_provider setting selects exactly one transport provider.",
    ),
    ControlRelationship(
        name="phase3_quote_migration",
        mode=RelationshipMode.chain,
        members=(
            "projects.crm_phase3_native_sync_enabled",
            "projects.quotes_native_read_enabled",
            "projects.quotes_native_write_enabled",
        ),
        rule="Sync precedes native reads; native reads precede the write flip.",
    ),
    ControlRelationship(
        name="notification_channels",
        mode=RelationshipMode.fanout,
        members=("web", "email", "push", "nextcloud", "whatsapp"),
        rule="Policy may select multiple channels; each channel selects one provider.",
    ),
    ControlRelationship(
        name="subscription_state_event_pipeline",
        mode=RelationshipMode.chain,
        members=(
            "internal_state_handlers",
            "customer_notifications",
            "external_integrations",
        ),
        rule="A failed state stage blocks later communication and external delivery.",
    ),
    ControlRelationship(
        name="workqueue_assignment",
        mode=RelationshipMode.competing,
        members=("manual_assignment", "routing_rule", "availability_assignment"),
        rule="Multiple candidates may compete, but one atomic owner claim wins.",
    ),
    ControlRelationship(
        name="scheduled_and_event_execution",
        mode=RelationshipMode.competing,
        members=("scheduler_trigger", "domain_event_trigger"),
        rule="Either trigger may request work; one idempotency/claim key executes it.",
    ),
    ControlRelationship(
        name="crm_native_writers",
        mode=RelationshipMode.incompatible,
        members=("crm_writer", "sub_native_writer"),
        rule="A vertical has one writer; CRM ingestion never writes back from Sub.",
    ),
    ControlRelationship(
        name="provisioning_writers",
        mode=RelationshipMode.incompatible,
        members=("legacy_direct_provisioner", "native_workflow_provisioner"),
        rule="A service transition is executed by one provisioning owner.",
    ),
)


HANDLER_CONTROLS: dict[str, HandlerControl] = {
    "LifecycleHandler": HandlerControl(
        "LifecycleHandler", HandlerStage.state, 10, ("subscription_lifecycle",)
    ),
    "ProvisioningHandler": HandlerControl(
        "ProvisioningHandler", HandlerStage.state, 40, ("service_provisioning",)
    ),
    "EnforcementHandler": HandlerControl(
        "EnforcementHandler", HandlerStage.state, 50, ("service_enforcement",)
    ),
    "ArrangementHandler": HandlerControl(
        "ArrangementHandler", HandlerStage.state, 20, ("payment_arrangements",)
    ),
    "ReferralHandler": HandlerControl(
        "ReferralHandler", HandlerStage.state, 30, ("referral_qualification",)
    ),
    "NotificationHandler": HandlerControl(
        "NotificationHandler",
        HandlerStage.communication,
        10,
        ("customer_notifications",),
    ),
    "WebhookHandler": HandlerControl(
        "WebhookHandler", HandlerStage.external, 10, ("external_webhooks",)
    ),
    "IntegrationHookHandler": HandlerControl(
        "IntegrationHookHandler",
        HandlerStage.external,
        20,
        ("internal_integration_hooks",),
    ),
    "CrmSyncHandler": HandlerControl(
        "CrmSyncHandler", HandlerStage.external, 30, ("crm_compatibility_sync",)
    ),
}

RELATIONSHIP_SETTING_KEYS = {
    (SettingDomain.billing, "payment_gateway_failover_enabled"),
    (SettingDomain.billing, "payment_gateway_primary_provider"),
    (SettingDomain.billing, "payment_gateway_secondary_provider"),
    (SettingDomain.projects, "crm_phase3_native_sync_enabled"),
    (SettingDomain.projects, "quotes_native_read_enabled"),
    (SettingDomain.projects, "quotes_native_write_enabled"),
}

CHAINED_EVENT_TYPES = {
    "subscription.activated",
    "subscription.suspended",
    "subscription.resumed",
    "subscription.canceled",
    "subscription.expired",
    "payment.received",
    "invoice.overdue",
    "service_order.assigned",
    "provisioning.completed",
    "provisioning.failed",
}


def event_relationship_mode(event_type: str) -> RelationshipMode:
    if event_type in CHAINED_EVENT_TYPES:
        return RelationshipMode.chain
    return RelationshipMode.fanout


def validate_and_order_handlers(handlers: Iterable[Any]) -> list[Any]:
    resolved = list(handlers)
    names = [handler.__class__.__name__ for handler in resolved]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ControlRelationshipError(
            f"Duplicate event handler registrations: {', '.join(duplicates)}"
        )
    missing = sorted(set(names) - set(HANDLER_CONTROLS))
    if missing:
        raise ControlRelationshipError(
            f"Event handlers missing control declarations: {', '.join(missing)}"
        )

    capability_owner: dict[str, str] = {}
    for name in names:
        for capability in HANDLER_CONTROLS[name].capabilities:
            previous = capability_owner.get(capability)
            if previous:
                raise ControlRelationshipError(
                    f"Exclusive event capability {capability} owned by "
                    f"{previous} and {name}"
                )
            capability_owner[capability] = name
    return sorted(
        resolved,
        key=lambda handler: (
            HANDLER_CONTROLS[handler.__class__.__name__].stage,
            HANDLER_CONTROLS[handler.__class__.__name__].order,
        ),
    )


def event_topology() -> list[dict[str, object]]:
    return [
        {
            "handler": control.handler_name,
            "stage": control.stage.name,
            "order": control.order,
            "relationship": control.relationship.value,
            "capabilities": list(control.capabilities),
        }
        for control in sorted(
            HANDLER_CONTROLS.values(), key=lambda item: (item.stage, item.order)
        )
    ]


def event_policies() -> dict[str, object]:
    return {
        "default": RelationshipMode.fanout.value,
        "overrides": dict.fromkeys(
            sorted(CHAINED_EVENT_TYPES), RelationshipMode.chain.value
        ),
    }


def _value(
    db: Session,
    ref: SettingRef,
    pending: tuple[SettingDomain, str, object] | None = None,
) -> object:
    if pending and pending[0] == ref.domain and pending[1] == ref.key:
        return pending[2]
    from app.services.settings_spec import resolve_value

    return resolve_value(db, ref.domain, ref.key)


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def audit_setting_relationships(
    db: Session,
    *,
    pending: tuple[SettingDomain, str, object] | None = None,
) -> list[ControlFinding]:
    findings: list[ControlFinding] = []
    failover = _enabled(
        _value(
            db,
            SettingRef(SettingDomain.billing, "payment_gateway_failover_enabled"),
            pending,
        )
    )
    primary = str(
        _value(
            db,
            SettingRef(SettingDomain.billing, "payment_gateway_primary_provider"),
            pending,
        )
        or ""
    )
    secondary = str(
        _value(
            db,
            SettingRef(SettingDomain.billing, "payment_gateway_secondary_provider"),
            pending,
        )
        or ""
    )
    if failover and primary and primary == secondary:
        findings.append(
            ControlFinding(
                code="payment_provider_not_exclusive",
                severity="error",
                message="Payment failover primary and secondary providers must differ.",
                members=(primary, secondary),
            )
        )

    sync_enabled = _enabled(
        _value(
            db,
            SettingRef(SettingDomain.projects, "crm_phase3_native_sync_enabled"),
            pending,
        )
    )
    read_enabled = _enabled(
        _value(
            db,
            SettingRef(SettingDomain.projects, "quotes_native_read_enabled"),
            pending,
        )
    )
    write_enabled = _enabled(
        _value(
            db,
            SettingRef(SettingDomain.projects, "quotes_native_write_enabled"),
            pending,
        )
    )
    if write_enabled and not read_enabled:
        findings.append(
            ControlFinding(
                code="quote_write_before_read_flip",
                severity="error",
                message="Native quote writes require the native read path.",
                members=("quotes_native_write_enabled", "quotes_native_read_enabled"),
            )
        )
    if read_enabled and not (sync_enabled or write_enabled):
        findings.append(
            ControlFinding(
                code="quote_read_without_freshness_source",
                severity="warning",
                message="Native quote reads have neither CRM delta sync nor native writes.",
                members=(
                    "crm_phase3_native_sync_enabled",
                    "quotes_native_read_enabled",
                    "quotes_native_write_enabled",
                ),
            )
        )
    return findings


def validate_setting_change(
    db: Session, domain: SettingDomain, key: str, value: object
) -> None:
    if (domain, key) not in RELATIONSHIP_SETTING_KEYS:
        return
    errors = [
        finding
        for finding in audit_setting_relationships(db, pending=(domain, key, value))
        if finding.severity == "error"
    ]
    if errors:
        raise ControlRelationshipError("; ".join(item.message for item in errors))


def relationship_manifest() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "mode": item.mode.value,
            "members": list(item.members),
            "rule": item.rule,
        }
        for item in CONTROL_RELATIONSHIPS
    ]
