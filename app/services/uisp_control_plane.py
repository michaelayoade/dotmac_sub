"""Capability-aware desired/observed control plane for UISP devices.

UISP's documented NMS integration used by this application is read-only. This
module therefore records intent, snapshots observations, and reports drift, but
never marks a requested mutation successful merely because work was queued.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import AccessType, Subscription
from app.models.network import CPEDevice, DeviceType, OLTDevice, OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.provisioning import ServiceOrder
from app.models.uisp_control import (
    UispConfigSnapshot,
    UispDeviceIntent,
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)

READ_ONLY_CAPABILITIES: dict[str, bool | str] = {
    "inventory": True,
    "topology": True,
    "firmware_inventory": True,
    "config_observation": "limited",
    "config_write": False,
    "wifi_write": False,
    "firmware_upgrade": False,
    "remote_access_write": False,
    "decommission_write": False,
}

_SECRET_KEYS = {"password", "passphrase", "secret", "token", "credential"}
_OBSERVABLE_PATHS = {
    "name",
    "model",
    "mac_address",
    "management_ip",
    "status",
    "firmware_version",
}
_DESIRED_TOP_LEVEL = {
    "name",
    "management_ip",
    "firmware_version",
    "wifi",
    "remote_access",
    "lifecycle",
}


class UispIntentError(ValueError):
    pass


def capabilities() -> dict[str, bool | str]:
    return dict(READ_ONLY_CAPABILITIES)


def redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in _SECRET_KEYS):
                redacted[key] = "[redacted]" if item else item
            else:
                redacted[key] = redact_config(item)
        return redacted
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def _validate_desired(desired: dict) -> dict:
    unknown = set(desired) - _DESIRED_TOP_LEVEL
    if unknown:
        raise UispIntentError(f"Unsupported UISP intent fields: {sorted(unknown)}")
    wifi = desired.get("wifi")
    if wifi is not None:
        if not isinstance(wifi, dict):
            raise UispIntentError("wifi intent must be an object")
        if any(key in wifi for key in ("password", "passphrase", "psk")):
            raise UispIntentError(
                "Plaintext Wi-Fi credentials are not stored; use password_ref"
            )
        unknown_wifi = set(wifi) - {"ssid", "password_ref"}
        if unknown_wifi:
            raise UispIntentError(
                f"Unsupported UISP Wi-Fi fields: {sorted(unknown_wifi)}"
            )
    return copy.deepcopy(desired)


def _snapshot(
    db: Session,
    intent: UispDeviceIntent,
    source: UispSnapshotSource,
    config: dict,
    revision: int | None,
) -> UispConfigSnapshot:
    snapshot = UispConfigSnapshot(
        intent=intent,
        source=source,
        revision=revision,
        config=redact_config(config),
        redacted=True,
    )
    db.add(snapshot)
    return snapshot


def _target(
    db: Session, target_type: UispIntentTargetType, target_id: UUID
) -> tuple[CPEDevice | OntUnit, UUID | None, str | None]:
    if target_type == UispIntentTargetType.cpe:
        cpe = db.get(CPEDevice, target_id)
        if cpe is None:
            raise UispIntentError("CPE device not found")
        if cpe.uisp_device_id is None and cpe.device_type != DeviceType.wireless_radio:
            raise UispIntentError("CPE device is not staged for UISP adoption")
        return cpe, cpe.subscription_id, cpe.uisp_device_id

    ont = db.get(OntUnit, target_id)
    if ont is None:
        raise UispIntentError("ONT not found")
    olt = db.get(OLTDevice, ont.olt_device_id) if ont.olt_device_id else None
    if (
        ont.uisp_device_id is None
        or olt is None
        or olt.uisp_device_id is None
        or str(olt.vendor or "").strip().lower() != "ubiquiti"
    ):
        raise UispIntentError("ONT is not managed by UISP")
    assignment = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .one_or_none()
    )
    return ont, assignment.subscription_id if assignment else None, ont.uisp_device_id


def stage_intent(
    db: Session,
    *,
    target_type: UispIntentTargetType,
    target_id: UUID,
    desired_config: dict,
    subscription_id: UUID | None = None,
    service_order_id: UUID | None = None,
    commit: bool = True,
) -> UispDeviceIntent:
    desired = _validate_desired(desired_config)
    _device, owned_subscription_id, uisp_device_id = _target(db, target_type, target_id)
    if (
        subscription_id is not None
        and owned_subscription_id is not None
        and subscription_id != owned_subscription_id
    ):
        raise UispIntentError("Target belongs to another subscription")
    resolved_subscription_id = subscription_id or owned_subscription_id
    intent = (
        db.query(UispDeviceIntent)
        .filter(
            UispDeviceIntent.target_type == target_type,
            UispDeviceIntent.target_id == target_id,
        )
        .one_or_none()
    )
    if intent is None:
        intent = UispDeviceIntent(
            target_type=target_type,
            target_id=target_id,
            subscription_id=resolved_subscription_id,
            service_order_id=service_order_id,
            uisp_device_id=uisp_device_id,
            desired_config=desired,
            desired_revision=1,
            status=UispIntentStatus.staged,
        )
        db.add(intent)
        db.flush()
    else:
        if intent.desired_config != desired:
            intent.desired_revision += 1
        intent.desired_config = desired
        intent.subscription_id = resolved_subscription_id
        intent.service_order_id = service_order_id or intent.service_order_id
        intent.uisp_device_id = uisp_device_id
        intent.status = UispIntentStatus.staged
        intent.last_error = None
    _snapshot(
        db,
        intent,
        UispSnapshotSource.desired,
        desired,
        intent.desired_revision,
    )
    if commit:
        db.commit()
        db.refresh(intent)
    else:
        db.flush()
    return intent


def _identification(device: dict) -> dict:
    value = device.get("identification") or device.get("deviceIdentification")
    return value if isinstance(value, dict) else {}


def normalize_observation(device: dict) -> dict:
    ident = _identification(device)
    raw_overview = device.get("overview")
    overview: dict[str, Any] = raw_overview if isinstance(raw_overview, dict) else {}
    firmware = (
        ident.get("firmwareVersion")
        or ident.get("firmware")
        or device.get("firmwareVersion")
        or overview.get("firmwareVersion")
    )
    management_ip = str(device.get("ipAddress") or "").split("/", 1)[0] or None
    return {
        "name": ident.get("name"),
        "model": ident.get("model"),
        "mac_address": ident.get("mac"),
        "management_ip": management_ip,
        "status": overview.get("status"),
        "firmware_version": firmware,
    }


def _drift(desired: dict, observed: dict) -> tuple[dict, list[str]]:
    differences: dict[str, dict[str, Any]] = {}
    unsupported: list[str] = []
    for key, expected in desired.items():
        if key not in _OBSERVABLE_PATHS:
            unsupported.append(key)
            continue
        actual = observed.get(key)
        if actual != expected:
            differences[key] = {
                "desired": redact_config(expected),
                "observed": actual,
            }
    return differences, sorted(unsupported)


def observe_intent(
    db: Session,
    intent: UispDeviceIntent,
    device: dict,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> UispDeviceIntent:
    observed_at = now or datetime.now(UTC)
    observed = normalize_observation(device)
    differences, unsupported = _drift(intent.desired_config or {}, observed)
    if intent.observed_config != observed:
        _snapshot(db, intent, UispSnapshotSource.observed, observed, None)
    intent.observed_config = observed
    intent.last_observed_at = observed_at
    intent.drift = {"differences": differences, "unsupported": unsupported}
    if unsupported:
        intent.status = UispIntentStatus.manual_required
        intent.last_error = (
            "UISP NMS API has no documented write/readback support for: "
            + ", ".join(unsupported)
        )
    elif differences:
        intent.status = UispIntentStatus.drifted
        intent.last_error = None
    else:
        intent.status = UispIntentStatus.verified
        intent.verified_revision = intent.desired_revision
        intent.last_verified_at = observed_at
        intent.last_error = None
    if commit:
        db.commit()
        db.refresh(intent)
    else:
        db.flush()
    return intent


def reconcile_inventory(
    db: Session,
    devices: list[dict],
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, int]:
    by_id = {
        str(_identification(device).get("id") or ""): device
        for device in devices
        if isinstance(device, dict)
    }
    result = {
        "observed": 0,
        "missing": 0,
        "verified": 0,
        "drifted": 0,
        "manual_required": 0,
        "failed": 0,
    }
    intents = (
        db.query(UispDeviceIntent)
        .filter(UispDeviceIntent.status != UispIntentStatus.decommissioned)
        .all()
    )
    for intent in intents:
        try:
            _device, owned_subscription_id, current_uisp_id = _target(
                db, intent.target_type, intent.target_id
            )
        except UispIntentError as exc:
            intent.status = UispIntentStatus.failed
            intent.last_error = str(exc)
            result["failed"] += 1
            continue
        if intent.subscription_id is None and owned_subscription_id is not None:
            intent.subscription_id = owned_subscription_id
        if current_uisp_id != intent.uisp_device_id:
            intent.uisp_device_id = current_uisp_id
        device = by_id.get(intent.uisp_device_id or "")
        if device is None:
            intent.status = UispIntentStatus.pending_observation
            result["missing"] += 1
            continue
        observe_intent(db, intent, device, now=now, commit=False)
        result["observed"] += 1
        result[intent.status.value] += 1
    if commit:
        db.commit()
    else:
        db.flush()
    return result


def request_apply(
    db: Session, intent: UispDeviceIntent, *, initiated_by: str | None = None
) -> NetworkOperation:
    operation = NetworkOperation(
        operation_type=(
            NetworkOperationType.router_config_push
            if intent.target_type == UispIntentTargetType.cpe
            else NetworkOperationType.ont_provision
        ),
        target_type=(
            NetworkOperationTargetType.cpe
            if intent.target_type == UispIntentTargetType.cpe
            else NetworkOperationTargetType.ont
        ),
        target_id=intent.target_id,
        status=NetworkOperationStatus.warning,
        correlation_key=f"uisp:{intent.id}:revision:{intent.desired_revision}",
        input_payload=redact_config(intent.desired_config),
        output_payload={"capabilities": capabilities(), "applied": False},
        error="UISP mutation is not supported by the documented read-only adapter",
        initiated_by=initiated_by,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    intent.status = UispIntentStatus.manual_required
    intent.last_error = operation.error
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def update_intent_desired(
    db: Session,
    intent: UispDeviceIntent,
    *,
    name: str | None = None,
    management_ip: str | None = None,
    firmware_version: str | None = None,
    wifi_ssid: str | None = None,
    wifi_password: str | None = None,
    remote_access_enabled: bool | None = None,
    lifecycle_state: str | None = None,
) -> UispDeviceIntent:
    """Merge an operator edit into intent without claiming device delivery."""
    desired = copy.deepcopy(intent.desired_config or {})
    for key, value in (
        ("name", name),
        ("management_ip", management_ip),
        ("firmware_version", firmware_version),
    ):
        if value is not None:
            cleaned = str(value).strip()
            if cleaned:
                desired[key] = cleaned
            else:
                desired.pop(key, None)
    if wifi_ssid is not None or wifi_password:
        wifi = dict(desired.get("wifi") or {})
        if wifi_ssid is not None:
            cleaned_ssid = str(wifi_ssid).strip()
            if not (1 <= len(cleaned_ssid) <= 32):
                raise UispIntentError("Wi-Fi SSID must be 1-32 characters")
            wifi["ssid"] = cleaned_ssid
        if wifi_password:
            if not (8 <= len(wifi_password) <= 63):
                raise UispIntentError("Wi-Fi password must be 8-63 characters")
            from app.services.credential_crypto import encrypt_credential

            encrypted = encrypt_credential(wifi_password)
            if not encrypted or encrypted.startswith("plain:"):
                raise UispIntentError(
                    "Credential encryption is required before staging a Wi-Fi password"
                )
            wifi["password_ref"] = encrypted
        desired["wifi"] = wifi
    if remote_access_enabled is not None:
        desired["remote_access"] = {"enabled": remote_access_enabled}
    if lifecycle_state is not None:
        cleaned_state = str(lifecycle_state).strip().lower()
        if cleaned_state not in {"active", "suspended", "decommissioned"}:
            raise UispIntentError("Invalid UISP lifecycle state")
        desired["lifecycle"] = {"state": cleaned_state}
    return stage_intent(
        db,
        target_type=intent.target_type,
        target_id=intent.target_id,
        desired_config=desired,
        subscription_id=intent.subscription_id,
        service_order_id=intent.service_order_id,
    )


def stage_from_service_order(
    db: Session, order: ServiceOrder, *, commit: bool = True
) -> UispDeviceIntent | None:
    context = dict(order.execution_context or {})
    desired = context.get("uisp_desired")
    desired = desired if isinstance(desired, dict) else {}
    if order.subscription_id is None:
        return None
    subscription = db.get(Subscription, order.subscription_id)
    if subscription is None:
        return None
    explicitly_uisp = "uisp_desired" in context
    fixed_wireless = bool(
        subscription.offer
        and subscription.offer.access_type == AccessType.fixed_wireless
    )

    target_type: UispIntentTargetType | None = None
    target_id: UUID | None = None
    if fixed_wireless:
        cpes = (
            db.query(CPEDevice)
            .filter(CPEDevice.subscription_id == subscription.id)
            .order_by(CPEDevice.created_at.desc())
            .all()
        )
        if len(cpes) == 1:
            target_type, target_id = UispIntentTargetType.cpe, cpes[0].id
    else:
        assignments = (
            db.query(OntAssignment)
            .filter(
                OntAssignment.subscription_id == subscription.id,
                OntAssignment.active.is_(True),
            )
            .all()
        )
        if len(assignments) == 1:
            ont = db.get(OntUnit, assignments[0].ont_unit_id)
            olt = (
                db.get(OLTDevice, ont.olt_device_id)
                if ont and ont.olt_device_id
                else None
            )
            if ont and ont.uisp_device_id and olt and olt.uisp_device_id:
                target_type, target_id = UispIntentTargetType.ont, ont.id

    if target_type is None or target_id is None:
        if not (fixed_wireless or explicitly_uisp):
            return None
        context["uisp_control"] = {
            "status": "awaiting_device_assignment",
            "subscription_id": str(subscription.id),
        }
        order.execution_context = context
        if commit:
            db.commit()
        else:
            db.flush()
        return None

    intent = stage_intent(
        db,
        target_type=target_type,
        target_id=target_id,
        desired_config=desired,
        subscription_id=subscription.id,
        service_order_id=order.id,
        commit=False,
    )
    context["uisp_control"] = {
        "status": intent.status.value,
        "intent_id": str(intent.id),
        "target_type": target_type.value,
        "target_id": str(target_id),
    }
    order.execution_context = context
    if commit:
        db.commit()
        db.refresh(intent)
    else:
        db.flush()
    return intent


def stage_pending_orders_for_subscription(
    db: Session, subscription_id: UUID, *, commit: bool = True
) -> list[UispDeviceIntent]:
    """Stage orders that were waiting for an exact UISP device assignment."""
    orders = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.subscription_id == subscription_id)
        .order_by(ServiceOrder.created_at.asc())
        .all()
    )
    intents: list[UispDeviceIntent] = []
    for order in orders:
        intent = stage_from_service_order(db, order, commit=False)
        if intent is not None:
            intents.append(intent)
    if commit:
        db.commit()
        for intent in intents:
            db.refresh(intent)
    else:
        db.flush()
    return intents
