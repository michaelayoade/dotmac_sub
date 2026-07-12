"""Recovery workflow for credential ciphertext whose encryption key is lost."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus, Subscription
from app.models.network import OntAssignment, OntUnit
from app.models.radius import RadiusClient
from app.models.radius_active_session import RadiusActiveSession
from app.services.audit_adapter import stage_audit_event
from app.services.credential_crypto import (
    encrypt_credential_with_key,
    get_encryption_key,
    get_previous_encryption_key,
)
from app.services.credential_key_rotation import (
    classify_credential_value_state,
    publish_credential_integrity_snapshot,
    scan_credential_encryption_integrity,
)
from app.services.network.ont_desired_config import (
    desired_config,
    get_desired_config_value,
    set_desired_config_value,
)
from app.services.radius import (
    _radius_client_ip_for_nas,
    external_radius_nas_client_ips,
    external_radius_nas_secret_inventory,
    remove_external_radius_nas_clients,
)
from app.services.subscription_lifecycle_policy import TERMINAL_SERVICE_STATUSES


class CredentialCleanupAction(StrEnum):
    decommission_nas = "decommission_nas"
    recover_nas_secret = "recover_nas_secret"
    clear_ont_wifi_password = "clear_ont_wifi_password"
    blocked_active_nas = "blocked_active_nas"
    blocked_nas_subscription = "blocked_nas_subscription"
    blocked_nas_live_session = "blocked_nas_live_session"
    unsupported_undecryptable = "unsupported_undecryptable"


_BLOCKED_ACTIONS = frozenset(
    {
        CredentialCleanupAction.blocked_active_nas,
        CredentialCleanupAction.blocked_nas_subscription,
        CredentialCleanupAction.blocked_nas_live_session,
        CredentialCleanupAction.unsupported_undecryptable,
    }
)


@dataclass(frozen=True)
class CredentialCleanupItem:
    action: CredentialCleanupAction
    entity_type: str
    entity_id: str
    client_ip: str | None = None
    external_radius_present: bool = False
    internal_radius_clients: int = 0
    normalize_nas_status: bool = False
    active_assignments: int = 0
    recovery_fingerprint: str | None = None
    requires_lifecycle_review: bool = False

    @property
    def blocked(self) -> bool:
        return self.action in _BLOCKED_ACTIONS

    def digest_value(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "client_ip": self.client_ip,
            "external_radius_present": self.external_radius_present,
            "internal_radius_clients": self.internal_radius_clients,
            "normalize_nas_status": self.normalize_nas_status,
            "active_assignments": self.active_assignments,
            "recovery_fingerprint": self.recovery_fingerprint,
            "requires_lifecycle_review": self.requires_lifecycle_review,
        }


@dataclass(frozen=True)
class CredentialCleanupPlan:
    items: tuple[CredentialCleanupItem, ...]
    undecryptable_total: int
    integrity: Any = field(repr=False, compare=False)

    @property
    def action_counts(self) -> dict[str, int]:
        counts = Counter(item.action.value for item in self.items)
        return {key: int(counts[key]) for key in sorted(counts)}

    @property
    def blocked(self) -> int:
        return sum(1 for item in self.items if item.blocked)

    @property
    def eligible(self) -> int:
        return len(self.items) - self.blocked

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            [item.digest_value() for item in self.items],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_digest": self.digest,
            "undecryptable_total": self.undecryptable_total,
            "eligible": self.eligible,
            "blocked": self.blocked,
            "actions": self.action_counts,
        }


@dataclass(frozen=True)
class CredentialCleanupResult:
    status: str
    execute: bool
    plan: CredentialCleanupPlan
    local_values_cleared: int = 0
    nas_statuses_normalized: int = 0
    internal_radius_clients_deactivated: int = 0
    external_radius_clients_removed: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "execute": self.execute,
            **self.plan.as_dict(),
            "local_values_cleared": self.local_values_cleared,
            "nas_statuses_normalized": self.nas_statuses_normalized,
            "internal_radius_clients_deactivated": (
                self.internal_radius_clients_deactivated
            ),
            "external_radius_clients_removed": self.external_radius_clients_removed,
            "reason": self.reason,
        }


def _credential_keys() -> tuple[bytes, ...]:
    return tuple(
        key
        for key in (get_encryption_key(), get_previous_encryption_key())
        if key is not None
    )


def _count_nas_dependencies(db: Session, device: NasDevice) -> tuple[int, int, int]:
    subscriptions = int(
        db.scalar(
            select(func.count(Subscription.id))
            .where(Subscription.provisioning_nas_device_id == device.id)
            .where(Subscription.status.not_in(TERMINAL_SERVICE_STATUSES))
        )
        or 0
    )
    device_ips = {
        str(value).strip()
        for value in (device.nas_ip, device.management_ip, device.ip_address)
        if str(value or "").strip()
    }
    session_filters = [RadiusActiveSession.nas_device_id == device.id]
    if device_ips:
        session_filters.append(RadiusActiveSession.nas_ip_address.in_(device_ips))
    sessions = int(
        db.scalar(
            select(func.count(RadiusActiveSession.id)).where(or_(*session_filters))
        )
        or 0
    )
    internal_clients = int(
        db.scalar(
            select(func.count(RadiusClient.id))
            .where(RadiusClient.nas_device_id == device.id)
            .where(RadiusClient.is_active.is_(True))
        )
        or 0
    )
    return subscriptions, sessions, internal_clients


def build_credential_lifecycle_cleanup_plan(
    db: Session,
    *,
    lock: bool = False,
) -> CredentialCleanupPlan:
    """Build an identity-free-output plan over recoverably safe cleanup actions."""
    integrity = scan_credential_encryption_integrity(db)
    keys = _credential_keys()
    nas_query = select(NasDevice).order_by(NasDevice.id)
    ont_query = select(OntUnit).order_by(OntUnit.id)
    if lock:
        nas_query = nas_query.with_for_update()
        ont_query = ont_query.with_for_update()

    nas_rows = [
        row
        for row in db.scalars(nas_query).all()
        if classify_credential_value_state(row.shared_secret, keys) == "undecryptable"
    ]
    client_ips: set[str] = set()
    for row in nas_rows:
        value = _radius_client_ip_for_nas(row)
        if value:
            client_ips.add(value)
    external_ips = external_radius_nas_client_ips(db, client_ips)
    secret_inventory = external_radius_nas_secret_inventory(db, client_ips)
    items: list[CredentialCleanupItem] = []

    for device in nas_rows:
        subscriptions, sessions, internal_clients = _count_nas_dependencies(db, device)
        client_ip: str | None = _radius_client_ip_for_nas(device) or None
        recovered_secret = secret_inventory.recoverable_secrets.get(client_ip or "")
        recovery_fingerprint = (
            hashlib.sha256(recovered_secret.encode("utf-8")).hexdigest()
            if recovered_secret
            else None
        )
        if sessions:
            action = CredentialCleanupAction.blocked_nas_live_session
        elif device.is_active or subscriptions:
            action = (
                CredentialCleanupAction.recover_nas_secret
                if recovered_secret
                else (
                    CredentialCleanupAction.blocked_active_nas
                    if device.is_active
                    else CredentialCleanupAction.blocked_nas_subscription
                )
            )
        else:
            action = CredentialCleanupAction.decommission_nas
        items.append(
            CredentialCleanupItem(
                action=action,
                entity_type="NasDevice",
                entity_id=str(device.id),
                client_ip=client_ip,
                external_radius_present=bool(client_ip in external_ips),
                internal_radius_clients=internal_clients,
                normalize_nas_status=(device.status != NasDeviceStatus.decommissioned),
                recovery_fingerprint=recovery_fingerprint,
                requires_lifecycle_review=bool(
                    action == CredentialCleanupAction.recover_nas_secret
                    and not device.is_active
                ),
            )
        )

    for ont in db.scalars(ont_query).all():
        wifi_password = get_desired_config_value(
            desired_config(ont), "wifi", "password"
        )
        if classify_credential_value_state(wifi_password, keys) != "undecryptable":
            continue
        active_assignments = int(
            db.scalar(
                select(func.count(OntAssignment.id))
                .where(OntAssignment.ont_unit_id == ont.id)
                .where(OntAssignment.active.is_(True))
            )
            or 0
        )
        items.append(
            CredentialCleanupItem(
                action=CredentialCleanupAction.clear_ont_wifi_password,
                entity_type="OntUnit",
                entity_id=str(ont.id),
                active_assignments=active_assignments,
            )
        )

    known = len(items)
    unsupported = max(int(integrity.totals["undecryptable"]) - known, 0)
    for index in range(unsupported):
        items.append(
            CredentialCleanupItem(
                action=CredentialCleanupAction.unsupported_undecryptable,
                entity_type="UnsupportedCredential",
                entity_id=str(index),
            )
        )
    items.sort(key=lambda item: (item.entity_type, item.entity_id, item.action.value))
    return CredentialCleanupPlan(
        items=tuple(items),
        undecryptable_total=int(integrity.totals["undecryptable"]),
        integrity=integrity,
    )


def _publish(plan: CredentialCleanupPlan, status: str) -> None:
    publish_credential_integrity_snapshot(
        plan.integrity,
        operation="cleanup",
        operation_status=status,
        extra_observations=(
            ("cleanup_eligible", "all", float(plan.eligible)),
            ("cleanup_blocked", "all", float(plan.blocked)),
        ),
    )


def cleanup_unrecoverable_credentials(
    db: Session,
    *,
    execute: bool = False,
    confirm_plan_digest: str | None = None,
) -> CredentialCleanupResult:
    """Safely remove credentials that cannot be recovered after key loss."""
    plan = build_credential_lifecycle_cleanup_plan(db, lock=execute)
    if plan.blocked:
        result = CredentialCleanupResult(
            status="blocked",
            execute=execute,
            plan=plan,
            reason="unsafe_lifecycle_dependencies",
        )
        _publish(plan, result.status)
        return result
    if not execute:
        result = CredentialCleanupResult(status="dry_run", execute=False, plan=plan)
        _publish(plan, result.status)
        return result
    if not confirm_plan_digest or confirm_plan_digest != plan.digest:
        result = CredentialCleanupResult(
            status="confirmation_required",
            execute=True,
            plan=plan,
            reason="plan_digest_mismatch",
        )
        _publish(plan, result.status)
        return result

    external_ips = {
        item.client_ip
        for item in plan.items
        if item.action == CredentialCleanupAction.decommission_nas
        and item.external_radius_present
        and item.client_ip
    }
    recovery_ips: set[str] = set()
    for item in plan.items:
        if item.action == CredentialCleanupAction.recover_nas_secret and item.client_ip:
            recovery_ips.add(item.client_ip)
    recovery_inventory = external_radius_nas_secret_inventory(db, recovery_ips)
    for item in plan.items:
        if item.action != CredentialCleanupAction.recover_nas_secret:
            continue
        secret = recovery_inventory.recoverable_secrets.get(item.client_ip or "")
        fingerprint = (
            hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret else None
        )
        if not secret or fingerprint != item.recovery_fingerprint:
            raise RuntimeError("External RADIUS NAS secret changed after planning")

    active_key = get_encryption_key()
    if active_key is None:
        raise RuntimeError("Credential encryption key is unavailable")
    external_removed = remove_external_radius_nas_clients(db, external_ips)
    values_cleared = 0
    statuses_normalized = 0
    clients_deactivated = 0

    for item in plan.items:
        if item.action == CredentialCleanupAction.decommission_nas:
            device = db.get(NasDevice, item.entity_id)
            if device is None:
                raise RuntimeError("NAS cleanup target disappeared after planning")
            if device.status != NasDeviceStatus.decommissioned:
                device.status = NasDeviceStatus.decommissioned
                statuses_normalized += 1
            device.shared_secret = None
            values_cleared += 1
            clients = db.scalars(
                select(RadiusClient)
                .where(RadiusClient.nas_device_id == device.id)
                .where(RadiusClient.is_active.is_(True))
            ).all()
            for client in clients:
                client.is_active = False
                clients_deactivated += 1
            stage_audit_event(
                db,
                action="credential_lifecycle_cleanup",
                entity_type="NasDevice",
                entity_id=str(device.id),
                metadata={
                    "credential_field": "shared_secret",
                    "lifecycle_action": "decommissioned",
                    "external_radius_removed": item.external_radius_present,
                    "internal_radius_clients_deactivated": len(clients),
                },
            )
            continue

        if item.action == CredentialCleanupAction.recover_nas_secret:
            device = db.get(NasDevice, item.entity_id)
            if device is None:
                raise RuntimeError("NAS recovery target disappeared after planning")
            secret = recovery_inventory.recoverable_secrets[item.client_ip or ""]
            device.shared_secret = encrypt_credential_with_key(secret, active_key)
            values_cleared += 1
            stage_audit_event(
                db,
                action="credential_lifecycle_cleanup",
                entity_type="NasDevice",
                entity_id=str(device.id),
                metadata={
                    "credential_field": "shared_secret",
                    "lifecycle_action": "recovered_from_external_radius",
                    "requires_lifecycle_review": item.requires_lifecycle_review,
                },
            )
            continue

        if item.action == CredentialCleanupAction.clear_ont_wifi_password:
            ont = db.get(OntUnit, item.entity_id)
            if ont is None:
                raise RuntimeError("ONT cleanup target disappeared after planning")
            set_desired_config_value(ont, "wifi.password", None)
            values_cleared += 1
            stage_audit_event(
                db,
                action="credential_lifecycle_cleanup",
                entity_type="OntUnit",
                entity_id=str(ont.id),
                metadata={
                    "credential_field": "desired_config.wifi.password",
                    "reset_required": True,
                    "active_assignments": item.active_assignments,
                },
            )

    db.commit()
    after = scan_credential_encryption_integrity(db)
    completed_plan = CredentialCleanupPlan(
        items=plan.items,
        undecryptable_total=int(after.totals["undecryptable"]),
        integrity=after,
    )
    result = CredentialCleanupResult(
        status="completed" if after.totals["undecryptable"] == 0 else "incomplete",
        execute=True,
        plan=completed_plan,
        local_values_cleared=values_cleared,
        nas_statuses_normalized=statuses_normalized,
        internal_radius_clients_deactivated=clients_deactivated,
        external_radius_clients_removed=external_removed,
        reason=(
            None
            if after.totals["undecryptable"] == 0
            else "undecryptable_credentials_remain"
        ),
    )
    _publish(completed_plan, result.status)
    return result
