"""Register a customer's wireless-radio MAC at install time.

Closes the unmatched-radio gap at the source: instead of waiting for the
15-minute UISP topology sync to guess a radio's owner from
``subscriptions.mac_address``, the installer (admin subscription page or the
CRM/field API) records the radio MAC at turn-up. Registration:

- validates the MAC (format + not already bound to a DIFFERENT subscriber),
- creates the ``cpe_devices`` row directly (``subscriber_id`` is known, so the
  NOT NULL owner invariant is satisfied by construction; ``uisp_device_id``
  stays NULL until the UISP sync confirms the device),
- stamps ``subscriptions.mac_address`` when it is empty, so the EXISTING
  uisp_sync MAC matcher (built from active subscriptions) links the radio on
  its next run without any change to uisp_sync,
- on a cross-subscriber conflict, rejects the registration AND opens a
  deduped ops-queue item (see app/services/unmatched_radio_queue.py) so the
  ambiguity becomes a work item instead of a silent failure.

KNOWN LIMITATION (uisp_sync adoption hook — follow-up contract): uisp_sync's
``_upsert_station`` looks rows up by ``uisp_device_id`` only, so until the
adoption hook lands (match an existing ``cpe_devices`` row by normalized MAC
where ``uisp_device_id IS NULL`` before creating a new one) the sync will
create its own row for a radio registered here once the subscription MAC
matches. The hourly unmatched-radio review retires the manual placeholder as
soon as a UISP-confirmed row for the same MAC/subscriber appears, so the
duplicate is transient. The expected end state is captured by an xfail test
in tests/test_unmatched_radio_queue.py.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceStatus, DeviceType
from app.services import unmatched_radio_queue
from app.services.audit_helpers import log_audit_event
from app.services.network._common import normalize_mac_address

logger = logging.getLogger(__name__)

SOURCE_ADMIN = "admin"
SOURCE_CRM_API = "crm_api"


class InvalidMacError(ValueError):
    """The supplied MAC is not a valid 12-hex-digit hardware address."""


class MacConflictError(Exception):
    """The MAC is already bound to a different subscriber."""

    def __init__(self, mac_display: str, conflicting_subscriber_ids: set):
        self.mac_display = mac_display
        self.conflicting_subscriber_ids = {
            str(value) for value in conflicting_subscriber_ids
        }
        super().__init__(
            f"MAC {mac_display} is already associated with another subscriber; "
            "an ops review item has been opened."
        )


@dataclass
class RadioRegistration:
    device: CPEDevice
    created: bool
    subscription_mac_stamped: bool
    warnings: list[str] = field(default_factory=list)


def compact_mac(value: str | None) -> str | None:
    """Bare lowercase hex form (semantics match uisp_sync._norm_mac)."""
    canonical = normalize_mac_address(value)
    if canonical is None:
        return None
    return canonical.replace(":", "").lower()


def _compact_mac_sql(column):
    """SQL expression producing the bare lowercase hex form of a MAC column.

    Uses only lower()/replace() so it evaluates identically on Postgres and
    the SQLite test schema.
    """
    expr = func.lower(column)
    for junk in (":", "-", ".", " "):
        expr = func.replace(expr, junk, "")
    return expr


_MAC_LOCK_NAMESPACE = "radio_mac"


def mac_lock_key(mac_compact: str) -> int:
    """Stable signed-bigint advisory-lock key for a normalized MAC.

    sha256-derived (NOT the builtin ``hash``, which is per-process salted) so
    every process/worker derives the same key for the same MAC.
    """
    digest = hashlib.sha256(f"{_MAC_LOCK_NAMESPACE}:{mac_compact}".encode()).digest()[
        :8
    ]
    return int.from_bytes(digest, byteorder="big", signed=True)


def acquire_mac_lock(db: Session, mac_compact: str) -> None:
    """Serialize check-then-write sections for one radio MAC.

    Takes ``pg_advisory_xact_lock`` on the caller's session (released
    automatically at commit/rollback), so a double-click on the admin form or
    a timeout-retry from the CRM client blocks until the first transaction
    finishes and then SEES its writes — the existence checks that follow the
    lock stay correct under concurrency. Reentrant within a transaction.
    No-op on non-PostgreSQL engines (SQLite tests), mirroring
    ``locking.serial_advisory_lock``.
    """
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": mac_lock_key(mac_compact)},
    )


def _resolve_subscription(db: Session, subscription_id: str) -> Subscription:
    try:
        key = uuid.UUID(str(subscription_id))
    except (TypeError, ValueError) as exc:
        raise LookupError("Subscription not found") from exc
    subscription = db.get(Subscription, key)
    if subscription is None:
        raise LookupError("Subscription not found")
    return subscription


def _cpe_rows_for_mac(db: Session, compact: str) -> list[CPEDevice]:
    return (
        db.query(CPEDevice)
        .filter(
            CPEDevice.mac_address.isnot(None),
            _compact_mac_sql(CPEDevice.mac_address) == compact,
        )
        .all()
    )


def _other_subscriber_owners(
    db: Session, compact: str, subscriber_id: uuid.UUID
) -> set:
    """Distinct OTHER subscribers this MAC is already bound to.

    Considers both the uisp_sync matcher's source of truth (ACTIVE
    subscriptions' mac_address) and existing cpe_devices rows (any non-retired
    row, UISP-confirmed or manually registered).
    """
    owners: set = set()
    subscription_rows = (
        db.query(Subscription.subscriber_id)
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.mac_address.isnot(None),
            _compact_mac_sql(Subscription.mac_address) == compact,
        )
        .all()
    )
    owners.update(row[0] for row in subscription_rows)
    for cpe in _cpe_rows_for_mac(db, compact):
        if cpe.status != DeviceStatus.retired:
            owners.add(cpe.subscriber_id)
    owners.discard(subscriber_id)
    owners.discard(None)
    return owners


def list_radios_for_subscriber(db: Session, subscriber_id) -> list[CPEDevice]:
    """Wireless-radio CPE rows for a subscriber, newest first."""
    if subscriber_id is None:
        return []
    return (
        db.query(CPEDevice)
        .filter(
            CPEDevice.subscriber_id == subscriber_id,
            CPEDevice.device_type == DeviceType.wireless_radio,
        )
        .order_by(CPEDevice.created_at.desc())
        .all()
    )


def register_radio_mac(
    db: Session,
    *,
    subscription_id: str,
    mac: str | None,
    actor_id: str | None = None,
    source: str = SOURCE_ADMIN,
    request=None,
) -> RadioRegistration:
    """Record a customer radio MAC against a subscription at install time.

    Raises LookupError (unknown subscription), InvalidMacError (bad format)
    or MacConflictError (MAC bound to a different subscriber; a deduped ops
    queue item is opened and committed before raising). Idempotent: re-posting
    the same MAC for the same subscriber returns the existing row.

    Commits on success and on the conflict path.
    """
    subscription = _resolve_subscription(db, subscription_id)
    canonical = normalize_mac_address(mac)
    if canonical is None:
        raise InvalidMacError(
            "Invalid MAC address; expected 12 hexadecimal digits "
            "(e.g. 24:A4:3C:AA:BB:01)."
        )
    compact = canonical.replace(":", "").lower()

    # Serialize the check-then-write below per MAC: a concurrent registration
    # of the same MAC (form double-click, CRM client timeout-retry) waits here
    # and then observes the first transaction's row, keeping the idempotency
    # and conflict checks race-free. Released on the commit/rollback paths of
    # this function.
    acquire_mac_lock(db, compact)

    owners = _other_subscriber_owners(db, compact, subscription.subscriber_id)
    if owners:
        unmatched_radio_queue.open_item(
            db,
            mac_compact=compact,
            reason=unmatched_radio_queue.REASON_CONFLICT,
            title=f"Radio MAC conflict: {canonical}",
            description=(
                f"Registration of radio MAC {canonical} for subscriber "
                f"{subscription.subscriber_id} (subscription {subscription.id}, "
                f"source {source}) was rejected: the MAC is already bound to "
                "another subscriber. Confirm which customer owns the radio and "
                "correct the stale binding."
            ),
            subscriber_id=subscription.subscriber_id,
            details={
                "subscription_id": str(subscription.id),
                "attempted_subscriber_id": str(subscription.subscriber_id),
                "conflicting_subscriber_ids": sorted(str(o) for o in owners),
                "source": source,
            },
        )
        log_audit_event(
            db=db,
            request=request,
            action="radio_mac_register_conflict",
            entity_type="subscription",
            entity_id=str(subscription.id),
            actor_id=actor_id,
            metadata={"mac_address": canonical, "source": source},
            status_code=409,
            is_success=False,
        )
        db.commit()
        raise MacConflictError(canonical, owners)

    warnings: list[str] = []
    stamped = False
    existing_sub_mac = compact_mac(subscription.mac_address)
    if existing_sub_mac is None:
        # Stamp the subscription so the EXISTING uisp_sync matcher (built from
        # active subscriptions' MACs) links this radio by construction.
        subscription.mac_address = canonical
        stamped = True
    elif existing_sub_mac != compact:
        warnings.append(
            "Subscription already carries a different MAC "
            f"({subscription.mac_address}); it was left unchanged."
        )

    device = next(
        (
            row
            for row in _cpe_rows_for_mac(db, compact)
            if row.subscriber_id == subscription.subscriber_id
        ),
        None,
    )
    created = device is None
    if device is None:
        device = CPEDevice(
            subscriber_id=subscription.subscriber_id,
            service_address_id=subscription.service_address_id,
            device_type=DeviceType.wireless_radio,
            mac_address=canonical,
            installed_at=datetime.now(UTC),
            notes=f"Radio MAC registered at install (source: {source}).",
        )
        db.add(device)
        db.flush()
    elif device.device_type in (None, DeviceType.other):
        device.device_type = DeviceType.wireless_radio

    # A successful (re-)registration resolves any lingering conflict review
    # item for this MAC; "not adopted by UISP" items stay open until the sync
    # actually confirms the device.
    unmatched_radio_queue.close_open_items_for_mac(
        db,
        mac_compact=compact,
        resolution=f"Radio MAC registered to subscriber {subscription.subscriber_id}.",
        reasons={unmatched_radio_queue.REASON_CONFLICT},
    )

    log_audit_event(
        db=db,
        request=request,
        action="radio_mac_register",
        entity_type="cpe_device",
        entity_id=str(device.id),
        actor_id=actor_id,
        metadata={
            "mac_address": canonical,
            "subscription_id": str(subscription.id),
            "created": created,
            "subscription_mac_stamped": stamped,
            "source": source,
        },
    )
    db.commit()
    db.refresh(device)
    return RadioRegistration(
        device=device,
        created=created,
        subscription_mac_stamped=stamped,
        warnings=warnings,
    )
