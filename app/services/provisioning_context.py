"""Single source of truth for provisioning operation context.

Provisioning workflows should not rediscover subscriber, ONT, CPE, TR-069, or
ACS context inside individual tasks/steps. This module composes that context
once from the network SOT and exposes a stable dict-enrichment contract for the
legacy step runner.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice, DeviceStatus, OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential
from app.services.network.subscriber_ont_adapter import (
    ProvisioningContext,
    resolve_provisioning_context,
)


def resolve_operations_provisioning_context(
    db: Session,
    *,
    subscriber_id: str | None = None,
    subscription_id: str | None = None,
    ont_id: str | None = None,
) -> ProvisioningContext:
    """Resolve the canonical provisioning context for operations workflows."""
    return resolve_provisioning_context(
        db,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        ont_id=ont_id,
    )


def extend_provisioning_context(
    db: Session,
    subscription_id: str | None,
    context: dict,
) -> dict:
    """Enrich a provisioning step/run context in-place and return it.

    The existing provisioning runner passes a mutable ``dict`` to step adapters.
    Keeping that contract avoids broad churn while centralizing the rules for
    which operational identifiers belong in that context.
    """
    if not subscription_id:
        return context
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return context

    canonical = resolve_operations_provisioning_context(
        db,
        subscriber_id=str(subscription.subscriber_id)
        if subscription.subscriber_id
        else None,
        subscription_id=str(subscription.id),
        ont_id=str(context["ont_id"]) if context.get("ont_id") else None,
    )
    _merge_canonical_context(context, canonical)
    _merge_cpe_context(db, subscription, context)
    _merge_acs_context(db, context)
    return context


def _merge_canonical_context(
    context: dict,
    canonical: ProvisioningContext,
) -> None:
    values = {
        "subscriber_id": canonical.subscriber_id,
        "subscription_id": canonical.subscription_id,
        "ont_id": canonical.ont_id,
        "ont_unit_id": canonical.ont_id,
        "ont_serial": canonical.ont_serial,
        "olt_id": canonical.olt_id,
        "olt_name": canonical.olt_name,
        "fsp": canonical.fsp,
        "ont_id_on_olt": canonical.ont_id_on_olt,
        "service_address_id": canonical.service_address_id,
        "nas_device_id": canonical.nas_device_id,
    }
    for key, value in values.items():
        if value is not None and not context.get(key):
            context[key] = value


def _merge_cpe_context(
    db: Session,
    subscription: Subscription,
    context: dict,
) -> None:
    device = (
        db.query(CPEDevice)
        .filter(CPEDevice.subscription_id == subscription.id)
        .filter(CPEDevice.status == DeviceStatus.active)
        .order_by(CPEDevice.created_at.desc())
        .first()
    )
    if not device:
        return
    context.update(
        {
            "cpe_device_id": str(device.id),
            "cpe_serial_number": device.serial_number,
        }
    )
    tr069_device = None
    if device.id:
        tr069_device = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.cpe_device_id == device.id)
            .first()
        )
    if not tr069_device and device.serial_number:
        tr069_device = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.serial_number == device.serial_number)
            .filter(Tr069CpeDevice.is_active.is_(True))
            .first()
        )
    if not tr069_device:
        return
    context.update(
        {
            "tr069_cpe_device_id": str(tr069_device.id),
            "tr069_serial_number": tr069_device.serial_number,
            "tr069_oui": tr069_device.oui,
            "tr069_product_class": tr069_device.product_class,
            "tr069_acs_server_id": str(tr069_device.acs_server_id),
        }
    )
    if tr069_device.oui and tr069_device.product_class and tr069_device.serial_number:
        context["genieacs_device_id"] = (
            f"{tr069_device.oui}-{tr069_device.product_class}-"
            f"{tr069_device.serial_number}"
        )


def _merge_acs_context(db: Session, context: dict) -> None:
    acs_server_id = context.get("tr069_acs_server_id")
    if not acs_server_id and context.get("ont_id"):
        ont = db.get(OntUnit, context["ont_id"])
        if ont and ont.olt_device_id:
            olt = db.get(OLTDevice, str(ont.olt_device_id))
            if olt and olt.tr069_acs_server_id:
                acs_server_id = str(olt.tr069_acs_server_id)
    if not acs_server_id:
        default_id = settings_spec.resolve_value(
            db, SettingDomain.tr069, "default_acs_server_id"
        )
        if default_id:
            acs_server_id = str(default_id)

    if not acs_server_id:
        return
    acs_server = db.get(Tr069AcsServer, acs_server_id)
    if acs_server and acs_server.cwmp_url:
        context["acs_server"] = {
            "cwmp_url": acs_server.cwmp_url,
            "cwmp_username": acs_server.cwmp_username,
            "cwmp_password": decrypt_credential(acs_server.cwmp_password),
        }
