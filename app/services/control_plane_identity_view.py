"""Read-only control-plane identity and adapter readiness projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.network_operation import NetworkOperation
from app.models.router_management import Router, RouterConfigPushResult
from app.models.uisp_control import (
    UispDeviceIntent,
    UispIntentStatus,
    UispIntentTargetType,
)
from app.services.device_adapter_binding import (
    ADAPTER_BINDING_PAYLOAD_KEY,
    AdapterBinding,
    DeviceIdentity,
)


@dataclass(frozen=True)
class ControlPlaneIdentityView:
    """Operator-facing projection of observed and operation-pinned identity."""

    provider: str
    binding: AdapterBinding | None
    observed_identity: DeviceIdentity | None
    pinned_binding: Mapping[str, Any] | None
    observed_at: datetime | None
    observed_source: str
    readiness: str
    readiness_label: str
    readiness_tone: str
    write_allowed: bool
    write_reason: str
    binding_changed: bool = False

    @property
    def identity(self):
        return self.observed_identity or (
            self.binding.identity if self.binding else None
        )

    @property
    def adapter_label(self) -> str:
        if self.binding is None:
            return "No adapter mapped"
        return f"{self.binding.adapter_name}@{self.binding.adapter_revision[:12]}"


def _pinned_binding(operation: NetworkOperation | None) -> Mapping[str, Any] | None:
    if operation is None or not isinstance(operation.input_payload, dict):
        return None
    raw = operation.input_payload.get(ADAPTER_BINDING_PAYLOAD_KEY)
    return raw if isinstance(raw, Mapping) else None


def _binding_changed(
    current: AdapterBinding | None, pinned: Mapping[str, Any] | None
) -> bool:
    if current is None or pinned is None:
        return False
    planned = str(pinned.get("binding_fingerprint") or "").strip()
    return bool(planned) and planned != current.fingerprint


def _latest_uisp_operation(
    db: Session, intent: UispDeviceIntent
) -> NetworkOperation | None:
    operations = (
        db.query(NetworkOperation)
        .filter(
            NetworkOperation.target_id == intent.target_id,
        )
        .order_by(NetworkOperation.created_at.desc())
        .limit(20)
        .all()
    )
    for operation in operations:
        payload = operation.input_payload or {}
        control = payload.get("_control_plane")
        if isinstance(control, dict) and control.get("provider") == "uisp":
            return operation
    return None


def _uisp_target(db: Session, intent: UispDeviceIntent) -> CPEDevice | OntUnit | None:
    if intent.target_type == UispIntentTargetType.cpe:
        return db.get(CPEDevice, intent.target_id)
    return db.get(OntUnit, intent.target_id)


def uisp_identity_view(
    db: Session,
    intent: UispDeviceIntent,
    *,
    profile: Any | None,
    capability_error: str | None,
) -> ControlPlaneIdentityView:
    target = _uisp_target(db, intent)
    operation = _latest_uisp_operation(db, intent)
    binding = getattr(profile, "binding", None)
    observed_identity = None
    if target is not None:
        try:
            observed_identity = DeviceIdentity.from_device(target)
        except ValueError:
            pass
    pinned = _pinned_binding(operation)
    changed = _binding_changed(binding, pinned)
    busy = intent.status in {
        UispIntentStatus.applying,
        UispIntentStatus.pending_readback,
    }

    if target is None:
        readiness, label, tone = "unavailable", "Target missing", "error"
        allowed, reason = False, "The UISP intent target no longer exists."
    elif capability_error:
        readiness, label, tone = "unmapped", "Mapping required", "error"
        allowed, reason = False, capability_error
    elif changed:
        readiness, label, tone = "identity_changed", "Re-plan required", "warning"
        allowed = bool(profile and profile.apply_ready and not busy)
        reason = (
            "Observed model, firmware, or capability mapping changed after the last "
            "operation was planned. Re-plan against the current identity."
        )
    elif profile is None or not profile.requested_fields:
        readiness, label, tone = "blocked", "No writable intent", "neutral"
        allowed, reason = False, "No mapped writable fields are staged."
    elif profile.unsupported_fields:
        readiness, label, tone = "blocked", "Unsupported fields", "warning"
        allowed = False
        reason = "Remove unsupported desired fields before applying."
    elif busy:
        readiness, label, tone = "in_progress", "Write in progress", "warning"
        allowed, reason = False, "A write or mandatory readback is already in progress."
    elif intent.status == UispIntentStatus.verified:
        readiness, label, tone = "verified", "Verified", "active"
        allowed, reason = False, "This desired revision is already verified."
    else:
        readiness, label, tone = "ready", "Ready to apply", "active"
        allowed, reason = True, "Current identity has an explicit writable mapping."

    observed_at = intent.last_observed_at
    if observed_at is None and target is not None:
        observed_at = getattr(target, "uisp_synced_at", None) or getattr(
            target, "last_seen_at", None
        )
    return ControlPlaneIdentityView(
        provider="UISP",
        binding=binding,
        observed_identity=observed_identity,
        pinned_binding=pinned,
        observed_at=observed_at,
        observed_source="UISP configuration readback",
        readiness=readiness,
        readiness_label=label,
        readiness_tone=tone,
        write_allowed=allowed,
        write_reason=reason,
        binding_changed=changed,
    )


def router_identity_view(
    router: Router, *, result: RouterConfigPushResult | None = None
) -> ControlPlaneIdentityView:
    from app.services.router_management.write_adapter import (
        RouterWriteUnsupported,
        routeros_adapter_binding,
    )

    try:
        observed_identity = DeviceIdentity.from_device(router, vendor="MikroTik")
    except ValueError:
        observed_identity = None
    try:
        binding = routeros_adapter_binding(router)
        error = None
    except RouterWriteUnsupported as exc:
        binding = None
        error = str(exc)
    pinned = _pinned_binding(result.operation if result else None)
    changed = _binding_changed(binding, pinned)
    if error:
        readiness, label, tone = "unmapped", "Mapping required", "error"
        allowed, reason = False, error
    elif changed:
        readiness, label, tone = "identity_changed", "Re-plan required", "warning"
        allowed = False
        reason = "Observed RouterOS identity changed after this push was planned."
    elif not router.is_active:
        readiness, label, tone = "blocked", "Router inactive", "neutral"
        allowed, reason = False, "Activate the router before configuration writes."
    else:
        readiness, label, tone = "ready", "Write mapped", "active"
        allowed, reason = (
            True,
            "RouterOS v7 REST writes have an explicit adapter mapping.",
        )
    return ControlPlaneIdentityView(
        provider="RouterOS",
        binding=binding,
        observed_identity=observed_identity,
        pinned_binding=pinned,
        observed_at=router.last_seen_at,
        observed_source="Router inventory sync",
        readiness=readiness,
        readiness_label=label,
        readiness_tone=tone,
        write_allowed=allowed,
        write_reason=reason,
        binding_changed=changed,
    )


def olt_identity_view(olt: OLTDevice) -> ControlPlaneIdentityView:
    from app.services.adapters.olt_types import olt_type_registry

    vendor = str(getattr(olt, "vendor", None) or "").strip()
    model = str(getattr(olt, "model", None) or "").strip()
    firmware = getattr(olt, "firmware_version", None)
    software = getattr(olt, "software_version", None)
    binding = None
    observed_identity = None
    if vendor and model:
        observed_identity = DeviceIdentity(
            vendor=vendor,
            model=model,
            firmware_version=firmware,
            software_version=software,
        )
        binding = olt_type_registry.resolve_binding(
            vendor=vendor,
            model=model,
            firmware=firmware,
            software_version=software,
        )
    generic = bool(binding and binding.adapter_name.endswith("-generic"))
    if not vendor or not model:
        readiness, label, tone = "unmapped", "Identity incomplete", "error"
        allowed, reason = False, "Observed OLT vendor and model are required."
    elif binding is None or generic:
        readiness, label, tone = "unmapped", "Read-only mapping", "warning"
        allowed = False
        reason = (
            "No firmware-specific OLT adapter is mapped; model-dependent writes "
            "remain disabled."
        )
    else:
        readiness, label, tone = "ready", "Write mapped", "active"
        allowed, reason = True, "OLT model and firmware resolve to an explicit adapter."
    last_poll_at = getattr(olt, "last_poll_at", None)
    observed_at = (
        last_poll_at
        or getattr(olt, "last_successful_ssh_at", None)
        or getattr(olt, "last_ping_at", None)
    )
    source = "OLT poll" if last_poll_at else "OLT SSH inventory"
    return ControlPlaneIdentityView(
        provider="Huawei OLT",
        binding=binding,
        observed_identity=observed_identity,
        pinned_binding=None,
        observed_at=observed_at,
        observed_source=source,
        readiness=readiness,
        readiness_label=label,
        readiness_tone=tone,
        write_allowed=allowed,
        write_reason=reason,
    )
