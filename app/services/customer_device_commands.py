"""Customer-scoped reboot and Wi-Fi command contract.

The network operation ledger owns device execution state.  This service owns
customer/subscription/device scope and presents one typed outcome to every
self-service adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.network import OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_actions import OntActions
from app.services.network.ont_features import OntFeatureService
from app.services.network_operations import commit_tracked_action, run_tracked_action
from app.services.settings_spec import resolve_value


class CustomerDeviceCommandKind(StrEnum):
    reboot = "reboot"
    wifi_update = "wifi_update"


class CustomerDeviceCommandStatus(StrEnum):
    succeeded = "succeeded"
    waiting = "waiting"
    failed = "failed"


@dataclass(frozen=True)
class CustomerDeviceCommandOutcome:
    command: CustomerDeviceCommandKind
    status: CustomerDeviceCommandStatus
    subscription_id: UUID
    device_id: UUID | None
    operation_id: UUID | None
    message: str

    @property
    def success(self) -> bool:
        return self.status == CustomerDeviceCommandStatus.succeeded


class CustomerDeviceCommandError(ValueError):
    """Stable, transport-neutral customer command rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _assigned_ont(
    db: Session, *, subscriber_id: UUID, subscription_id: UUID
) -> tuple[Subscription, OntUnit]:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None or subscription.subscriber_id != subscriber_id:
        raise CustomerDeviceCommandError("subscription_not_found", "Service not found")
    if subscription.status != SubscriptionStatus.active:
        raise CustomerDeviceCommandError(
            "subscription_inactive", "Only active services support device commands"
        )
    assignment = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.subscription_id == subscription.id,
            OntAssignment.subscriber_id == subscriber_id,
            OntAssignment.active.is_(True),
        )
        .one_or_none()
    )
    if assignment is None or assignment.ont_unit is None:
        raise CustomerDeviceCommandError(
            "device_not_assigned", "No active device is linked to this service"
        )
    if assignment.ont_unit.uisp_device_id:
        raise CustomerDeviceCommandError(
            "device_command_unsupported",
            "Self-service device commands are not supported for this device",
        )
    return subscription, assignment.ont_unit


def _outcome(
    *,
    command: CustomerDeviceCommandKind,
    subscription_id: UUID,
    device_id: UUID,
    result: object,
) -> CustomerDeviceCommandOutcome:
    data = getattr(result, "data", None) or {}
    raw_operation_id = data.get("operation_id")
    return CustomerDeviceCommandOutcome(
        command=command,
        status=(
            CustomerDeviceCommandStatus.waiting
            if bool(getattr(result, "waiting", False))
            else CustomerDeviceCommandStatus.succeeded
            if bool(getattr(result, "success", False))
            else CustomerDeviceCommandStatus.failed
        ),
        subscription_id=subscription_id,
        device_id=device_id,
        operation_id=UUID(str(raw_operation_id)) if raw_operation_id else None,
        message=str(getattr(result, "message", "Device command submitted")),
    )


def reboot_subscription_device(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    actor_id: str,
) -> CustomerDeviceCommandOutcome:
    """Reboot the exact non-UISP ONT currently assigned to the subscription."""
    _subscription, ont = _assigned_ont(
        db, subscriber_id=subscriber_id, subscription_id=subscription_id
    )
    cooldown_value = resolve_value(
        db, SettingDomain.network, "customer_ont_reboot_cooldown_seconds"
    )
    try:
        cooldown_seconds = (
            int(str(cooldown_value)) if cooldown_value is not None else 300
        )
    except (TypeError, ValueError):
        cooldown_seconds = 300
    if cooldown_seconds > 0:
        latest = (
            db.query(NetworkOperation.created_at)
            .filter(
                NetworkOperation.operation_type == NetworkOperationType.ont_reboot,
                NetworkOperation.target_type == NetworkOperationTargetType.ont,
                NetworkOperation.target_id == ont.id,
                NetworkOperation.status.notin_(
                    [NetworkOperationStatus.failed, NetworkOperationStatus.canceled]
                ),
            )
            .order_by(NetworkOperation.created_at.desc())
            .first()
        )
        if latest and latest.created_at:
            created_at = latest.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            remaining = int(
                cooldown_seconds - (datetime.now(UTC) - created_at).total_seconds()
            )
            if remaining > 0:
                minutes = max(1, -(-remaining // 60))
                raise CustomerDeviceCommandError(
                    "reboot_cooldown",
                    "Your device was restarted recently. Please wait about "
                    f"{minutes} minute{'s' if minutes != 1 else ''} before trying again.",
                )
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_reboot,
        NetworkOperationTargetType.ont,
        str(ont.id),
        lambda: OntActions.reboot(db, str(ont.id)),
        correlation_key=f"customer:{subscription_id}:reboot:{ont.id}",
        initiated_by=f"customer:{actor_id}",
    )
    outcome = _outcome(
        command=CustomerDeviceCommandKind.reboot,
        subscription_id=subscription_id,
        device_id=ont.id,
        result=result,
    )
    commit_tracked_action(db)
    return outcome


def update_subscription_wifi(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    actor_id: str,
    ssid: str,
    password: str | None,
) -> CustomerDeviceCommandOutcome:
    """Apply and verify Wi-Fi state on the exact assigned non-UISP ONT."""
    ssid_value = ssid.strip()
    password_value = password.strip() if password else None
    if not 1 <= len(ssid_value) <= 32:
        raise CustomerDeviceCommandError(
            "invalid_wifi_name", "WiFi name must be 1-32 characters"
        )
    if password_value is not None and not 8 <= len(password_value) <= 63:
        raise CustomerDeviceCommandError(
            "invalid_wifi_password", "WiFi password must be 8-63 characters"
        )
    _subscription, ont = _assigned_ont(
        db, subscriber_id=subscriber_id, subscription_id=subscription_id
    )
    result = run_tracked_action(
        db,
        NetworkOperationType.wifi_update,
        NetworkOperationTargetType.ont,
        str(ont.id),
        lambda: OntFeatureService.set_wifi_config(
            db, str(ont.id), ssid=ssid_value, password=password_value
        ),
        correlation_key=f"customer:{subscription_id}:wifi:{ont.id}",
        initiated_by=f"customer:{actor_id}",
    )
    outcome = _outcome(
        command=CustomerDeviceCommandKind.wifi_update,
        subscription_id=subscription_id,
        device_id=ont.id,
        result=result,
    )
    commit_tracked_action(db)
    return outcome
