"""Exact-service IPv4 assignment lifecycle and projection repair.

``IPAssignment`` is the desired-address authority. This owner contains both the
safe legacy ownership-link migration and the reviewed exact-service assignment
repair command. A separate fingerprinted command converges the served IPv4
projection after the ledger is exact; its durable event requests RADIUS and
old-IP session consequences after commit.

Preview is exhaustive and read-only.  Confirmation is limited to rows where:

* the assignment has no ``subscription_id``;
* the assignment has one subscriber;
* that subscriber has exactly one active subscription;
* that subscriber has exactly one active IPv4 assignment; and
* the subscription's served IPv4 equals the assignment address.

Every other row remains an explicitly classified blocker for operator review.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPv6Address,
    IPVersion,
    OLTDevice,
    SubscriberAdditionalRoute,
)
from app.models.network_monitoring import NetworkDevice
from app.models.radius import RadiusClient
from app.models.radius_active_session import RadiusActiveSession
from app.models.router_management import Router
from app.models.subscriber import Subscriber
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    current_command_context,
    current_command_definition,
    execute_owner_command,
    owner_command_active,
)

_OWNER = "network.ip_assignment_lifecycle"
_CONCERN = "exact service ownership of active IPv4 assignments"
_OWNERSHIP_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_ip_assignment_service_ownership",
)
_LIFECYCLE_CONCERN = "reviewed exact-service IPv4 assignment lifecycle repair"
_LIFECYCLE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="repair_service_ipv4_assignment",
)
_PROJECTION_CONCERN = "reviewed exact-service IPv4 served projection repair"
_PROJECTION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_PROJECTION_CONCERN,
    name="repair_service_ipv4_projection",
)
_REPLACE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="replace_service_ipv4",
)
# Like `replace_service_ipv4`, these reuse a declared concern instead of
# widening the manifest in the same diff.
_IPV6_LINK_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="link_service_ipv6_assignment",
)
_UNLINK_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="unlink_ip_assignment_service_ownership",
)
_ITEM_AUDIT_ACTION = "network.ip_assignment.service_ownership_linked"
_BATCH_AUDIT_ACTION = "network.ip_assignment.service_ownership_reconciled"
_LIFECYCLE_ITEM_AUDIT_ACTION = "network.ip_assignment.lifecycle_item_changed"
_LIFECYCLE_BATCH_AUDIT_ACTION = "network.ip_assignment.lifecycle_repaired"
_PROJECTION_BATCH_AUDIT_ACTION = "network.ip_assignment.served_projection_repaired"
_IPV6_LIFECYCLE_BATCH_AUDIT_ACTION = "network.ip_assignment.ipv6_lifecycle_repaired"
_UNLINK_ITEM_AUDIT_ACTION = "network.ip_assignment.service_ownership_unlinked"
_UNLINK_BATCH_AUDIT_ACTION = "network.ip_assignment.service_ownership_unlinked_batch"
_RETAINING_STATUSES = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.archived,
        SubscriptionStatus.blocked,
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.pending,
        SubscriptionStatus.paused,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    }
)
# Statuses a retention-expired release may free. Deliberately separate from
# both sets above: active, blocked, suspended, pending, paused and stopped are
# never released by retention.
_RETENTION_RELEASE_STATUSES = frozenset(
    {
        SubscriptionStatus.archived,
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    }
)


class IPAssignmentOwnershipDecision(StrEnum):
    exact = "exact"
    repairable_missing_service_link = "repairable_missing_service_link"
    missing_subscriber = "missing_subscriber"
    missing_subscription = "missing_subscription"
    subscriber_mismatch = "subscriber_mismatch"
    served_address_mismatch = "served_address_mismatch"
    ambiguous_active_services = "ambiguous_active_services"
    ambiguous_active_assignments = "ambiguous_active_assignments"


class IPAssignmentOwnershipError(DomainError):
    """Stable fail-closed service-ownership reconciliation error."""


class IPv4AssignmentRepairDecision(StrEnum):
    ready_create = "ready_create"
    ready_link = "ready_link"
    ready_keep = "ready_keep"
    ready_release = "ready_release"
    ready_release_retention_expired = "ready_release_retention_expired"
    retention_not_expired = "retention_not_expired"
    address_in_live_use = "address_in_live_use"
    address_not_releasable = "address_not_releasable"
    noop = "noop"
    subscription_not_found = "subscription_not_found"
    subscriber_not_found = "subscriber_not_found"
    subscription_not_serviceable = "subscription_not_serviceable"
    subscription_not_terminal = "subscription_not_terminal"
    target_address_not_found = "target_address_not_found"
    target_address_not_serviceable = "target_address_not_serviceable"
    target_address_in_routed_block = "target_address_in_routed_block"
    target_address_is_device_host = "target_address_is_device_host"
    target_owned_by_other_service = "target_owned_by_other_service"
    deactivation_assignment_not_found = "deactivation_assignment_not_found"
    deactivation_not_exact_service = "deactivation_not_exact_service"
    desired_assignment_selected_for_deactivation = (
        "desired_assignment_selected_for_deactivation"
    )
    incomplete_deactivation_set = "incomplete_deactivation_set"


class IPv4AssignmentLifecycleError(DomainError):
    """Stable fail-closed exact-service IPv4 assignment lifecycle error."""


class IPv4ServedProjectionDecision(StrEnum):
    ready = "ready"
    noop = "noop"
    subscription_not_found = "subscription_not_found"
    subscription_not_active = "subscription_not_active"
    missing_exact_assignment = "missing_exact_assignment"
    multiple_exact_assignments = "multiple_exact_assignments"
    assignment_subscriber_mismatch = "assignment_subscriber_mismatch"
    missing_login = "missing_login"
    shared_login_not_selected = "shared_login_not_selected"
    radius_observation_unavailable = "radius_observation_unavailable"
    radius_projection_not_aligned = "radius_projection_not_aligned"
    session_observation_conflict = "session_observation_conflict"


@dataclass(frozen=True, slots=True)
class ActiveIPv4AssignmentEvidence:
    assignment_id: UUID
    subscriber_id: UUID | None
    subscription_id: UUID | None
    address_id: UUID
    address: str


@dataclass(frozen=True, slots=True)
class ServiceIPv4AssignmentRepairPreview:
    subscription_id: UUID
    subscriber_id: UUID | None
    desired_address_id: UUID | None
    desired_address: str | None
    deactivate_assignment_ids: tuple[UUID, ...]
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...]
    decision: IPv4AssignmentRepairDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision in {
            IPv4AssignmentRepairDecision.ready_create,
            IPv4AssignmentRepairDecision.ready_link,
            IPv4AssignmentRepairDecision.ready_keep,
            IPv4AssignmentRepairDecision.ready_release,
            IPv4AssignmentRepairDecision.ready_release_retention_expired,
        }


@dataclass(frozen=True, slots=True)
class RepairServiceIPv4AssignmentCommand:
    context: CommandContext
    subscription_id: UUID
    desired_address_id: UUID | None
    deactivate_assignment_ids: tuple[UUID, ...]
    preview_fingerprint: str
    #: Retention-expiry release only. Computed by the caller from the retention
    #: setting (now - days). Required for a disabled/hidden/archived release.
    retention_cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class ServiceIPv4AssignmentRepairOutcome:
    subscription_id: UUID
    desired_assignment_id: UUID | None
    linked_count: int
    created_count: int
    deactivated_count: int
    preview_fingerprint: str
    replayed: bool


class IPv6AssignmentLinkDecision(StrEnum):
    ready_link = "ready_link"
    noop = "noop"
    subscription_not_found = "subscription_not_found"
    subscriber_not_found = "subscriber_not_found"
    subscription_not_serviceable = "subscription_not_serviceable"
    target_address_not_found = "target_address_not_found"
    target_address_not_serviceable = "target_address_not_serviceable"
    target_assignment_not_found = "target_assignment_not_found"
    target_owned_by_other_service = "target_owned_by_other_service"
    service_already_has_ipv6_assignment = "service_already_has_ipv6_assignment"


@dataclass(frozen=True, slots=True)
class ActiveIPv6AssignmentEvidence:
    assignment_id: UUID
    subscriber_id: UUID | None
    subscription_id: UUID | None
    address_id: UUID
    address: str


@dataclass(frozen=True, slots=True)
class ServiceIPv6AssignmentLinkPreview:
    subscription_id: UUID
    subscriber_id: UUID | None
    desired_address_id: UUID
    desired_address: str | None
    active_assignments: tuple[ActiveIPv6AssignmentEvidence, ...]
    decision: IPv6AssignmentLinkDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is IPv6AssignmentLinkDecision.ready_link


@dataclass(frozen=True, slots=True)
class LinkServiceIPv6AssignmentCommand:
    context: CommandContext
    subscription_id: UUID
    desired_address_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ServiceIPv6AssignmentLinkOutcome:
    subscription_id: UUID
    desired_assignment_id: UUID | None
    linked_count: int
    preview_fingerprint: str
    replayed: bool


class IPAssignmentServiceUnlinkDecision(StrEnum):
    ready_unlink = "ready_unlink"
    assignment_not_found = "assignment_not_found"
    assignment_not_active = "assignment_not_active"
    assignment_not_linked = "assignment_not_linked"
    subscription_not_found = "subscription_not_found"
    subscription_mismatch = "subscription_mismatch"
    subscriber_mismatch = "subscriber_mismatch"


@dataclass(frozen=True, slots=True)
class ServiceIPAssignmentUnlinkPreview:
    assignment_id: UUID
    subscriber_id: UUID | None
    current_subscription_id: UUID | None
    expected_subscription_id: UUID
    subscription_status: str | None
    subscription_service_address_id: UUID | None
    ip_version: str | None
    address_id: UUID | None
    address: str | None
    served_address: str | None
    served_address_matches_assignment: bool
    # IPv4 only: find_ipv4_live_use has no IPv6 equivalent, so IPv6 previews
    # leave this unchecked/None instead of inventing session semantics.
    live_session_checked: bool
    live_session_present: bool | None
    is_primary: bool
    service_address_id: UUID | None
    decision: IPAssignmentServiceUnlinkDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is IPAssignmentServiceUnlinkDecision.ready_unlink


@dataclass(frozen=True, slots=True)
class UnlinkIPAssignmentServiceOwnershipCommand:
    context: CommandContext
    assignment_id: UUID
    expected_subscription_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class IPAssignmentServiceUnlinkOutcome:
    assignment_id: UUID
    previous_subscription_id: UUID | None
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ServiceIPv4ProjectionPreview:
    subscription_id: UUID
    subscriber_id: UUID | None
    assignment_id: UUID | None
    served_address: str | None
    desired_address: str | None
    radius_mode: str | None
    observed_radius_address: str | None
    active_session_count: int
    old_address_session_count: int
    decision: IPv4ServedProjectionDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is IPv4ServedProjectionDecision.ready


@dataclass(frozen=True, slots=True)
class RepairServiceIPv4ProjectionCommand:
    context: CommandContext
    subscription_id: UUID
    assignment_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ServiceIPv4ProjectionOutcome:
    subscription_id: UUID
    assignment_id: UUID
    event_id: UUID
    previous_address: str
    desired_address: str
    observed_active_sessions: int
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipItem:
    assignment_id: UUID
    subscriber_id: UUID | None
    current_subscription_id: UUID | None
    proposed_subscription_id: UUID | None
    address: str
    decision: IPAssignmentOwnershipDecision

    @property
    def repairable(self) -> bool:
        return (
            self.decision
            is IPAssignmentOwnershipDecision.repairable_missing_service_link
        )


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipPreview:
    items: tuple[IPAssignmentOwnershipItem, ...]
    fingerprint: str

    @property
    def counts(self) -> dict[IPAssignmentOwnershipDecision, int]:
        counts = Counter(item.decision for item in self.items)
        return {
            decision: counts.get(decision, 0)
            for decision in IPAssignmentOwnershipDecision
        }

    @property
    def repairable_assignment_ids(self) -> tuple[UUID, ...]:
        return tuple(item.assignment_id for item in self.items if item.repairable)


@dataclass(frozen=True, slots=True)
class ReconcileIPAssignmentOwnershipCommand:
    context: CommandContext
    preview_fingerprint: str
    assignment_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class IPAssignmentOwnershipOutcome:
    preview_fingerprint: str
    linked_count: int
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise IPAssignmentOwnershipError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _normalized_ip(value: str | None) -> str:
    return str(value or "").strip()


def _preview_payload(
    items: tuple[IPAssignmentOwnershipItem, ...],
) -> list[dict[str, str | None]]:
    return [
        {
            "assignment_id": str(item.assignment_id),
            "subscriber_id": (
                str(item.subscriber_id) if item.subscriber_id is not None else None
            ),
            "current_subscription_id": (
                str(item.current_subscription_id)
                if item.current_subscription_id is not None
                else None
            ),
            "proposed_subscription_id": (
                str(item.proposed_subscription_id)
                if item.proposed_subscription_id is not None
                else None
            ),
            "address": item.address,
            "decision": item.decision.value,
        }
        for item in items
    ]


def _fingerprint(items: tuple[IPAssignmentOwnershipItem, ...]) -> str:
    encoded = json.dumps(
        _preview_payload(items),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_ip_assignment_service_ownership(
    db: Session,
    *,
    assignment_ids: tuple[UUID, ...] | None = None,
) -> IPAssignmentOwnershipPreview:
    """Classify active IPv4 assignment ownership without changing state."""

    requested_ids = set(assignment_ids) if assignment_ids is not None else None
    assignment_rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.is_active.is_(True),
                IPAssignment.ip_version == IPVersion.ipv4,
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    assignments = [assignment for assignment, _address in assignment_rows]
    addresses_by_assignment = {
        assignment.id: _normalized_ip(address)
        for assignment, address in assignment_rows
    }
    active_subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.active)
            .order_by(Subscription.id)
        ).all()
    )
    subscriptions_by_id = {row.id: row for row in active_subscriptions}
    subscriptions_by_subscriber: dict[UUID, list[Subscription]] = defaultdict(list)
    for subscription in active_subscriptions:
        subscriptions_by_subscriber[subscription.subscriber_id].append(subscription)
    assignments_by_subscriber: dict[UUID, list[IPAssignment]] = defaultdict(list)
    for assignment in assignments:
        if assignment.subscriber_id is not None:
            assignments_by_subscriber[assignment.subscriber_id].append(assignment)

    items: list[IPAssignmentOwnershipItem] = []
    for assignment in assignments:
        if requested_ids is not None and assignment.id not in requested_ids:
            continue
        address = addresses_by_assignment[assignment.id]
        subscriber_id = assignment.subscriber_id
        current_subscription_id = assignment.subscription_id
        proposed_subscription_id: UUID | None = None

        if subscriber_id is None:
            decision = IPAssignmentOwnershipDecision.missing_subscriber
        elif current_subscription_id is not None:
            linked_subscription = subscriptions_by_id.get(current_subscription_id)
            if linked_subscription is None:
                decision = IPAssignmentOwnershipDecision.missing_subscription
            elif linked_subscription.subscriber_id != subscriber_id:
                decision = IPAssignmentOwnershipDecision.subscriber_mismatch
            elif _normalized_ip(linked_subscription.ipv4_address) != address:
                decision = IPAssignmentOwnershipDecision.served_address_mismatch
            elif len(assignments_by_subscriber[subscriber_id]) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_assignments
            else:
                decision = IPAssignmentOwnershipDecision.exact
        else:
            subscriptions = subscriptions_by_subscriber.get(subscriber_id, [])
            if len(subscriptions) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_services
            elif len(assignments_by_subscriber[subscriber_id]) != 1:
                decision = IPAssignmentOwnershipDecision.ambiguous_active_assignments
            else:
                subscription = subscriptions[0]
                if _normalized_ip(subscription.ipv4_address) != address:
                    decision = IPAssignmentOwnershipDecision.served_address_mismatch
                else:
                    proposed_subscription_id = subscription.id
                    decision = (
                        IPAssignmentOwnershipDecision.repairable_missing_service_link
                    )
        items.append(
            IPAssignmentOwnershipItem(
                assignment_id=assignment.id,
                subscriber_id=subscriber_id,
                current_subscription_id=current_subscription_id,
                proposed_subscription_id=proposed_subscription_id,
                address=address,
                decision=decision,
            )
        )

    result = tuple(items)
    return IPAssignmentOwnershipPreview(items=result, fingerprint=_fingerprint(result))


def _lifecycle_error(suffix: str, message: str, **details: object) -> NoReturn:
    raise IPv4AssignmentLifecycleError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _repair_preview_payload(
    *,
    subscription_id: UUID,
    subscriber_id: UUID | None,
    desired_address_id: UUID | None,
    desired_address: str | None,
    deactivate_assignment_ids: tuple[UUID, ...],
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...],
    decision: IPv4AssignmentRepairDecision,
    retention_cutoff: datetime | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "desired_address_id": (
            str(desired_address_id) if desired_address_id is not None else None
        ),
        "desired_address": desired_address,
        "deactivate_assignment_ids": [
            str(value) for value in deactivate_assignment_ids
        ],
        "active_assignments": [
            {
                "assignment_id": str(item.assignment_id),
                "subscriber_id": (
                    str(item.subscriber_id) if item.subscriber_id is not None else None
                ),
                "subscription_id": (
                    str(item.subscription_id)
                    if item.subscription_id is not None
                    else None
                ),
                "address_id": str(item.address_id),
                "address": item.address,
            }
            for item in active_assignments
        ],
        "decision": decision.value,
    }
    if retention_cutoff is not None:
        # Only present for a retention release, so existing fingerprints are
        # unchanged.
        payload["retention_cutoff"] = _aware(retention_cutoff).isoformat()
    return payload


def _repair_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _address_candidates(addresses: set[str]) -> set[str]:
    return addresses | {f"{value}/32" for value in addresses}


@dataclass(frozen=True, slots=True)
class IPv4LiveUseEvidence:
    live_session: bool
    sibling_subscription_ids: tuple[UUID, ...]

    @property
    def in_use(self) -> bool:
        return self.live_session or bool(self.sibling_subscription_ids)


def find_ipv4_live_use(
    db: Session,
    *,
    subscription_id: UUID,
    addresses: set[str],
    exclude_own_sessions: bool = False,
) -> IPv4LiveUseEvidence:
    """Read-only: is any of these addresses in use by a live session or by the
    served column of another non-terminal subscription?

    ``exclude_own_sessions`` ignores sessions attributed to ``subscription_id``
    itself (an allocator re-checking its own service's address); sessions with
    no subscription attribution still count.
    """

    candidates = _address_candidates(
        {str(value).strip().split("/", 1)[0] for value in addresses if value}
    )
    if not candidates:
        return IPv4LiveUseEvidence(False, ())
    session_query = select(RadiusActiveSession.id).where(
        RadiusActiveSession.framed_ip_address.in_(candidates)
    )
    if exclude_own_sessions:
        session_query = session_query.where(
            or_(
                RadiusActiveSession.subscription_id.is_(None),
                RadiusActiveSession.subscription_id != subscription_id,
            )
        )
    live_session = db.scalar(session_query.limit(1)) is not None
    siblings = tuple(
        db.scalars(
            select(Subscription.id)
            .where(
                Subscription.ipv4_address.in_(candidates),
                Subscription.id != subscription_id,
                Subscription.status.not_in(_TERMINAL_STATUSES),
            )
            .order_by(Subscription.id)
        )
    )
    return IPv4LiveUseEvidence(live_session, siblings)


def project_initial_served_ipv4(
    db: Session, *, subscription: Subscription, address: str
) -> Subscription:
    """Owner write for a just-created service: set the served column from its
    own active primary IPAM assignment. Refuses unless the column is empty and the
    address is that subscription's active primary IPv4 assignment, so it can
    never move a served customer."""

    bare = str(address).strip().split("/", 1)[0]
    if str(subscription.ipv4_address or "").strip():
        _lifecycle_error(
            "served_ipv4_already_set",
            "The service already has a served IPv4; use Replace IPv4.",
        )
    held = [
        (str(value).split("/", 1)[0], bool(primary))
        for value, primary in db.execute(
            select(IPv4Address.address, IPAssignment.is_primary)
            .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == subscription.id,
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.is_active.is_(True),
            )
        ).all()
    ]
    if (bare, True) not in held:
        _lifecycle_error(
            "address_not_own_assignment",
            "The address is not this service's active primary IPv4 assignment.",
        )
    if find_ipv4_live_use(
        db,
        subscription_id=subscription.id,
        addresses={bare},
        exclude_own_sessions=True,
    ).in_use:
        _lifecycle_error(
            "served_ipv4_held_elsewhere",
            "The address is held by another live service; not projecting it.",
        )
    _project_served_ipv4(subscription, bare)
    db.flush()
    return subscription


def ipv4_held_by_other_service_ipam(
    db: Session, *, subscription_id: UUID, address: str
) -> bool:
    """Read-only: does another non-terminal service hold ``address`` as an
    active IPAM assignment? ``x/32`` stored forms match."""

    bare = str(address).strip().split("/", 1)[0]
    if not bare:
        return False
    return (
        db.scalar(
            select(IPAssignment.id)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .join(Subscription, IPAssignment.subscription_id == Subscription.id)
            .where(
                IPAssignment.is_active.is_(True),
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.subscription_id != subscription_id,
                Subscription.status.not_in(_TERMINAL_STATUSES),
                IPv4Address.address.in_(_address_candidates({bare})),
            )
            .limit(1)
        )
        is not None
    )


def _retention_release_decision(
    db: Session,
    *,
    subscription: Subscription,
    exact_rows: list[ActiveIPv4AssignmentEvidence],
    retention_cutoff: datetime,
) -> IPv4AssignmentRepairDecision:
    """Gate a retention release on age, live use and address class.

    Status-change evidence is ``Subscription.updated_at`` for every status:
    ``canceled_at``/``end_at`` can predate the status change, and no
    timestamp exists for disabled/hidden/archived. Any later edit moves
    ``updated_at`` forward, so it can only DELAY a release, never hasten one.
    """

    changed_at = subscription.updated_at
    if changed_at is None or _aware(changed_at) > _aware(retention_cutoff):
        return IPv4AssignmentRepairDecision.retention_not_expired
    addresses = {item.address for item in exact_rows}
    for address_id in {item.address_id for item in exact_rows}:
        row = db.get(IPv4Address, address_id)
        if (
            row is None
            or row.ont_unit_id is not None
            or row.is_reserved
            or (str(row.allocation_type or "").strip().lower() == "management")
        ):
            return IPv4AssignmentRepairDecision.address_not_releasable
    live = find_ipv4_live_use(db, subscription_id=subscription.id, addresses=addresses)
    if live.in_use:
        return IPv4AssignmentRepairDecision.address_in_live_use
    return IPv4AssignmentRepairDecision.ready_release_retention_expired


def _active_assignment_evidence(
    db: Session,
    *,
    subscriber_id: UUID,
    desired_address_id: UUID | None,
) -> tuple[ActiveIPv4AssignmentEvidence, ...]:
    ownership_filter = IPAssignment.subscriber_id == subscriber_id
    if desired_address_id is not None:
        ownership_filter = or_(
            ownership_filter,
            IPAssignment.ipv4_address_id == desired_address_id,
        )
    rows = db.execute(
        select(IPAssignment, IPv4Address.address)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(
            IPAssignment.is_active.is_(True),
            IPAssignment.ip_version == IPVersion.ipv4,
            ownership_filter,
        )
        .order_by(IPAssignment.id)
    ).all()
    return tuple(
        ActiveIPv4AssignmentEvidence(
            assignment_id=assignment.id,
            subscriber_id=assignment.subscriber_id,
            subscription_id=assignment.subscription_id,
            address_id=assignment.ipv4_address_id,
            address=_normalized_ip(address),
        )
        for assignment, address in rows
        if assignment.ipv4_address_id is not None
    )


def _address_is_in_active_routed_block(db: Session, address: str) -> bool:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return True
    for cidr in db.scalars(
        select(SubscriberAdditionalRoute.cidr).where(
            SubscriberAdditionalRoute.is_active.is_(True)
        )
    ).all():
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network) and parsed in network:
            return True
    return False


# Free-text markers for an infrastructure pool. This is a backstop, NOT the
# primary signal: it only catches pools whose operator happened to spell the
# purpose out. `mgmt` and `mgt` are here because the abbreviation is the common
# hand-created form and "management" does not match it.
_INFRASTRUCTURE_POOL_MARKERS = ("management", "mgmt", "mgt")


def _pool_is_infrastructure(pool: IpPool) -> bool:
    """True when the pool serves devices rather than customers.

    `IpPool` has no typed purpose column, so this reads two signals:

    * `olt_device_id` -- typed and reliable. Every auto-created OLT management
      pool carries it, and customer pools served by an OLT do not; verified
      against production 2026-07-26, where all 7 pools with this FK were
      management pools and none of the 163 customer pools had it.
    * name/notes markers -- a backstop for hand-created infrastructure pools,
      which have no typed signal at all.

    The typed check comes first deliberately. Relying on the text alone made a
    safety decision depend on a naming convention: a pool called `OLT-MGMT`
    does not contain "management", so an OLT management address would have
    been assignable to a customer.
    """
    if pool.olt_device_id is not None:
        return True
    description = f"{pool.name} {pool.notes or ''}".strip().lower()
    return any(marker in description for marker in _INFRASTRUCTURE_POOL_MARKERS)


def _target_address_is_serviceable(
    address: IPv4Address,
    pool: IpPool | None,
) -> bool:
    if address.is_reserved or address.ont_unit_id is not None:
        return False
    if str(address.allocation_type or "").strip().lower() == "management":
        return False
    if pool is None or not pool.is_active or pool.ip_version != IPVersion.ipv4:
        return False
    return not _pool_is_infrastructure(pool)


def _target_address_is_device_host(db: Session, address: str) -> bool:
    candidates = (address, f"{address}/32")
    lookups = (
        select(NetworkDevice.id)
        .where(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.mgmt_ip.in_(candidates),
        )
        .limit(1),
        select(NasDevice.id)
        .where(
            NasDevice.is_active.is_(True),
            or_(
                NasDevice.management_ip.in_(candidates),
                NasDevice.ip_address.in_(candidates),
                NasDevice.nas_ip.in_(candidates),
            ),
        )
        .limit(1),
        select(OLTDevice.id)
        .where(
            OLTDevice.is_active.is_(True),
            OLTDevice.mgmt_ip.in_(candidates),
        )
        .limit(1),
        select(Router.id).where(Router.management_ip.in_(candidates)).limit(1),
        select(RadiusClient.id)
        .where(
            RadiusClient.is_active.is_(True),
            RadiusClient.client_ip.in_(candidates),
        )
        .limit(1),
    )
    return any(db.scalar(statement) is not None for statement in lookups)


def mark_primary_ipv4_assignment(
    db: Session,
    *,
    subscription_id: UUID,
    ipv4_address_id: UUID | None,
) -> bool:
    """Move the served-address marker to one active exact-service assignment.

    THE single writer of ``IPAssignment.is_primary``. Which held address a
    service is served on is an ownership decision, so no adapter may set the
    flag directly -- see the architecture guard in
    ``tests/architecture/test_ipv4_primary_marker_ownership.py``.

    Demotes any sibling first and flushes, because
    ``uq_ip_assignments_primary_ipv4_active`` permits only one active primary
    per service and promoting before demoting would violate it mid-statement.

    Returns True when a target assignment was found and marked.
    """
    if ipv4_address_id is None:
        # An assignment with no address cannot be the served one. Callers pass
        # `IPAssignment.ipv4_address_id` directly, which is nullable, so this
        # refuses rather than making the caller prove non-null at every site.
        return False

    active_ipv4 = (
        db.execute(
            select(IPAssignment)
            .where(IPAssignment.subscription_id == subscription_id)
            .where(IPAssignment.is_active.is_(True))
            .where(IPAssignment.ipv4_address_id.is_not(None))
        )
        .scalars()
        .all()
    )
    target = next(
        (item for item in active_ipv4 if item.ipv4_address_id == ipv4_address_id),
        None,
    )
    if target is None:
        return False

    demoted = False
    for item in active_ipv4:
        if item.id != target.id and item.is_primary:
            item.is_primary = False
            demoted = True
    if demoted:
        db.flush()

    if not target.is_primary:
        target.is_primary = True
        db.flush()
    return True


def preview_service_ipv4_assignment_repair(
    db: Session,
    *,
    subscription_id: UUID,
    desired_address_id: UUID | None,
    deactivate_assignment_ids: tuple[UUID, ...] = (),
    retention_cutoff: datetime | None = None,
) -> ServiceIPv4AssignmentRepairPreview:
    """Read-only, exact-service plan for one reviewed IPv4 assignment repair."""

    normalized_deactivate_ids = tuple(sorted(set(deactivate_assignment_ids), key=str))
    subscription = db.get(Subscription, subscription_id)
    subscriber_id = subscription.subscriber_id if subscription is not None else None
    desired_address: str | None = None
    active_assignments: tuple[ActiveIPv4AssignmentEvidence, ...] = ()
    decision = IPv4AssignmentRepairDecision.subscription_not_found

    if subscription is not None and subscriber_id is None:
        decision = IPv4AssignmentRepairDecision.subscriber_not_found
    elif (
        subscription is not None
        and subscriber_id is not None
        and db.get(Subscriber, subscriber_id) is None
    ):
        decision = IPv4AssignmentRepairDecision.subscriber_not_found
    elif subscription is not None and subscriber_id is not None:
        active_assignments = _active_assignment_evidence(
            db,
            subscriber_id=subscriber_id,
            desired_address_id=desired_address_id,
        )
        assignments_by_id = {item.assignment_id: item for item in active_assignments}
        requested_rows = [
            assignments_by_id.get(value) for value in normalized_deactivate_ids
        ]
        if any(item is None for item in requested_rows):
            decision = IPv4AssignmentRepairDecision.deactivation_assignment_not_found
        elif any(
            item is not None and item.subscription_id != subscription.id
            for item in requested_rows
        ):
            decision = IPv4AssignmentRepairDecision.deactivation_not_exact_service
        elif desired_address_id is None:
            exact_rows = [
                item
                for item in active_assignments
                if item.subscription_id == subscription.id
            ]
            remaining_exact = [
                item
                for item in exact_rows
                if item.assignment_id not in normalized_deactivate_ids
            ]
            # With a cutoff, canceled/expired releases take the same gated path
            # (age, live-use, reserved/management refusal) as the others.
            retention_release = retention_cutoff is not None and (
                subscription.status in _RETENTION_RELEASE_STATUSES | _TERMINAL_STATUSES
            )
            if subscription.status not in _TERMINAL_STATUSES and not retention_release:
                decision = IPv4AssignmentRepairDecision.subscription_not_terminal
            elif remaining_exact:
                decision = IPv4AssignmentRepairDecision.incomplete_deactivation_set
            elif not exact_rows:
                decision = IPv4AssignmentRepairDecision.noop
            elif retention_release:
                assert retention_cutoff is not None
                decision = _retention_release_decision(
                    db,
                    subscription=subscription,
                    exact_rows=exact_rows,
                    retention_cutoff=retention_cutoff,
                )
            else:
                decision = IPv4AssignmentRepairDecision.ready_release
        else:
            target = db.get(IPv4Address, desired_address_id)
            if subscription.status not in _RETAINING_STATUSES:
                decision = IPv4AssignmentRepairDecision.subscription_not_serviceable
            elif target is None:
                decision = IPv4AssignmentRepairDecision.target_address_not_found
            else:
                desired_address = _normalized_ip(target.address)
                pool = db.get(IpPool, target.pool_id) if target.pool_id else None
                target_assignment = next(
                    (
                        item
                        for item in active_assignments
                        if item.address_id == desired_address_id
                    ),
                    None,
                )
                remaining_exact = [
                    item
                    for item in active_assignments
                    if item.subscription_id == subscription.id
                    and item.address_id != desired_address_id
                    and item.assignment_id not in normalized_deactivate_ids
                ]
                if not _target_address_is_serviceable(target, pool):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_not_serviceable
                    )
                elif _address_is_in_active_routed_block(db, desired_address):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_in_routed_block
                    )
                elif _target_address_is_device_host(db, desired_address):
                    decision = (
                        IPv4AssignmentRepairDecision.target_address_is_device_host
                    )
                elif target_assignment is not None and (
                    target_assignment.subscriber_id != subscriber_id
                    or target_assignment.subscription_id not in {None, subscription.id}
                ):
                    decision = (
                        IPv4AssignmentRepairDecision.target_owned_by_other_service
                    )
                elif (
                    target_assignment is not None
                    and target_assignment.assignment_id in normalized_deactivate_ids
                ):
                    decision = IPv4AssignmentRepairDecision.desired_assignment_selected_for_deactivation
                elif remaining_exact:
                    decision = IPv4AssignmentRepairDecision.incomplete_deactivation_set
                elif target_assignment is None:
                    decision = IPv4AssignmentRepairDecision.ready_create
                elif target_assignment.subscription_id is None:
                    decision = IPv4AssignmentRepairDecision.ready_link
                elif normalized_deactivate_ids:
                    decision = IPv4AssignmentRepairDecision.ready_keep
                else:
                    decision = IPv4AssignmentRepairDecision.noop

    payload = _repair_preview_payload(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        desired_address_id=desired_address_id,
        desired_address=desired_address,
        deactivate_assignment_ids=normalized_deactivate_ids,
        active_assignments=active_assignments,
        decision=decision,
        retention_cutoff=retention_cutoff,
    )
    return ServiceIPv4AssignmentRepairPreview(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        desired_address_id=desired_address_id,
        desired_address=desired_address,
        deactivate_assignment_ids=normalized_deactivate_ids,
        active_assignments=active_assignments,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _active_ipv6_assignment_evidence(
    db: Session,
    *,
    subscriber_id: UUID,
    desired_address_id: UUID,
) -> tuple[ActiveIPv6AssignmentEvidence, ...]:
    ownership_filter = or_(
        IPAssignment.subscriber_id == subscriber_id,
        IPAssignment.ipv6_address_id == desired_address_id,
    )
    rows = db.execute(
        select(IPAssignment, IPv6Address.address)
        .join(IPv6Address, IPAssignment.ipv6_address_id == IPv6Address.id)
        .where(
            IPAssignment.is_active.is_(True),
            IPAssignment.ip_version == IPVersion.ipv6,
            ownership_filter,
        )
        .order_by(IPAssignment.id)
    ).all()
    return tuple(
        ActiveIPv6AssignmentEvidence(
            assignment_id=assignment.id,
            subscriber_id=assignment.subscriber_id,
            subscription_id=assignment.subscription_id,
            address_id=assignment.ipv6_address_id,
            address=_normalized_ip(address),
        )
        for assignment, address in rows
        if assignment.ipv6_address_id is not None
    )


def _target_ipv6_address_is_serviceable(
    address: IPv6Address,
    pool: IpPool | None,
) -> bool:
    # IPv6Address has no ONT binding or allocation type, unlike IPv4Address.
    if address.is_reserved:
        return False
    if pool is None or not pool.is_active or pool.ip_version != IPVersion.ipv6:
        return False
    return not _pool_is_infrastructure(pool)


def preview_service_ipv6_assignment_link(
    db: Session,
    *,
    subscription_id: UUID,
    desired_address_id: UUID,
) -> ServiceIPv6AssignmentLinkPreview:
    """Read-only, exact-service plan for one reviewed IPv6 assignment link.

    Link-only twin of the IPv4 ``ready_link`` repair branch: an existing
    active IPv6 assignment for the same subscriber is linked to the target
    service. Nothing is created, released, or deactivated, the served IPv6
    column is never consulted or written, and ``is_primary`` is never set
    (the primary marker and its partial-unique index are IPv4-only).
    """

    subscription = db.get(Subscription, subscription_id)
    subscriber_id = subscription.subscriber_id if subscription is not None else None
    desired_address: str | None = None
    active_assignments: tuple[ActiveIPv6AssignmentEvidence, ...] = ()
    decision = IPv6AssignmentLinkDecision.subscription_not_found

    if subscription is not None and subscriber_id is None:
        decision = IPv6AssignmentLinkDecision.subscriber_not_found
    elif (
        subscription is not None
        and subscriber_id is not None
        and db.get(Subscriber, subscriber_id) is None
    ):
        decision = IPv6AssignmentLinkDecision.subscriber_not_found
    elif subscription is not None and subscriber_id is not None:
        active_assignments = _active_ipv6_assignment_evidence(
            db,
            subscriber_id=subscriber_id,
            desired_address_id=desired_address_id,
        )
        target = db.get(IPv6Address, desired_address_id)
        if subscription.status not in _RETAINING_STATUSES:
            decision = IPv6AssignmentLinkDecision.subscription_not_serviceable
        elif target is None:
            decision = IPv6AssignmentLinkDecision.target_address_not_found
        else:
            desired_address = _normalized_ip(target.address)
            pool = db.get(IpPool, target.pool_id) if target.pool_id else None
            target_assignment = next(
                (
                    item
                    for item in active_assignments
                    if item.address_id == desired_address_id
                ),
                None,
            )
            remaining_exact = [
                item
                for item in active_assignments
                if item.subscription_id == subscription.id
                and item.address_id != desired_address_id
            ]
            if not _target_ipv6_address_is_serviceable(target, pool):
                decision = IPv6AssignmentLinkDecision.target_address_not_serviceable
            elif target_assignment is None:
                decision = IPv6AssignmentLinkDecision.target_assignment_not_found
            elif (
                target_assignment.subscriber_id != subscriber_id
                or target_assignment.subscription_id not in {None, subscription.id}
            ):
                decision = IPv6AssignmentLinkDecision.target_owned_by_other_service
            elif remaining_exact:
                decision = (
                    IPv6AssignmentLinkDecision.service_already_has_ipv6_assignment
                )
            elif target_assignment.subscription_id is None:
                decision = IPv6AssignmentLinkDecision.ready_link
            else:
                decision = IPv6AssignmentLinkDecision.noop

    payload: dict[str, object] = {
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "desired_address_id": str(desired_address_id),
        "desired_address": desired_address,
        "active_assignments": [
            {
                "assignment_id": str(item.assignment_id),
                "subscriber_id": (
                    str(item.subscriber_id) if item.subscriber_id is not None else None
                ),
                "subscription_id": (
                    str(item.subscription_id)
                    if item.subscription_id is not None
                    else None
                ),
                "address_id": str(item.address_id),
                "address": item.address,
            }
            for item in active_assignments
        ],
        "decision": decision.value,
    }
    return ServiceIPv6AssignmentLinkPreview(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        desired_address_id=desired_address_id,
        desired_address=desired_address,
        active_assignments=active_assignments,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def preview_ip_assignment_service_unlink(
    db: Session,
    *,
    assignment_id: UUID,
    expected_subscription_id: UUID,
) -> ServiceIPAssignmentUnlinkPreview:
    """Read-only plan to undo one exact service link (IPv4 or IPv6).

    Fingerprints the full linked state, so apply refuses if anything the
    rollback depends on moved after preview. The served column and (IPv4)
    live-session evidence are fingerprint-bound the same way, but for
    evidence only: a live session or a matching served address never makes
    the preview inapplicable, so rollback stays possible. There is no IPv6
    equivalent of ``find_ipv4_live_use``, so IPv6 previews report the live
    session as unchecked instead of inventing session semantics.
    """

    assignment = db.get(IPAssignment, assignment_id)
    subscriber_id = assignment.subscriber_id if assignment is not None else None
    current_subscription_id = (
        assignment.subscription_id if assignment is not None else None
    )
    ip_version = assignment.ip_version.value if assignment is not None else None
    address_id: UUID | None = None
    address: str | None = None
    is_primary = bool(assignment.is_primary) if assignment is not None else False
    service_address_id = (
        assignment.service_address_id if assignment is not None else None
    )
    subscription = (
        db.get(Subscription, current_subscription_id)
        if current_subscription_id is not None
        else None
    )
    subscription_status = (
        subscription.status.value if subscription is not None else None
    )
    subscription_service_address_id = (
        subscription.service_address_id if subscription is not None else None
    )
    decision = IPAssignmentServiceUnlinkDecision.assignment_not_found

    if assignment is not None:
        address_row: IPv4Address | IPv6Address | None
        if assignment.ip_version == IPVersion.ipv6:
            address_id = assignment.ipv6_address_id
            address_row = (
                db.get(IPv6Address, address_id) if address_id is not None else None
            )
        else:
            address_id = assignment.ipv4_address_id
            address_row = (
                db.get(IPv4Address, address_id) if address_id is not None else None
            )
        address = _normalized_ip(address_row.address) if address_row else None
        if not assignment.is_active:
            decision = IPAssignmentServiceUnlinkDecision.assignment_not_active
        elif current_subscription_id is None:
            decision = IPAssignmentServiceUnlinkDecision.assignment_not_linked
        elif current_subscription_id != expected_subscription_id:
            decision = IPAssignmentServiceUnlinkDecision.subscription_mismatch
        elif subscription is None:
            decision = IPAssignmentServiceUnlinkDecision.subscription_not_found
        elif subscription.subscriber_id != subscriber_id:
            decision = IPAssignmentServiceUnlinkDecision.subscriber_mismatch
        else:
            decision = IPAssignmentServiceUnlinkDecision.ready_unlink

    served_address: str | None = None
    if assignment is not None and subscription is not None:
        served_value = (
            subscription.ipv6_address
            if assignment.ip_version == IPVersion.ipv6
            else subscription.ipv4_address
        )
        served_address = _normalized_ip(served_value) or None
    served_address_matches_assignment = (
        served_address is not None and served_address == address
    )
    live_session_checked = False
    live_session_present: bool | None = None
    if (
        assignment is not None
        and assignment.ip_version == IPVersion.ipv4
        and current_subscription_id is not None
        and address
    ):
        live_session_checked = True
        live_session_present = find_ipv4_live_use(
            db,
            subscription_id=current_subscription_id,
            addresses={address},
            exclude_own_sessions=False,
        ).live_session

    payload: dict[str, object] = {
        "assignment_id": str(assignment_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "current_subscription_id": (
            str(current_subscription_id)
            if current_subscription_id is not None
            else None
        ),
        "expected_subscription_id": str(expected_subscription_id),
        "subscription_status": subscription_status,
        "subscription_service_address_id": (
            str(subscription_service_address_id)
            if subscription_service_address_id is not None
            else None
        ),
        "ip_version": ip_version,
        "address_id": str(address_id) if address_id is not None else None,
        "address": address,
        "served_address": served_address,
        "served_address_matches_assignment": served_address_matches_assignment,
        "live_session_checked": live_session_checked,
        "live_session_present": live_session_present,
        "is_primary": is_primary,
        "service_address_id": (
            str(service_address_id) if service_address_id is not None else None
        ),
        "decision": decision.value,
    }
    return ServiceIPAssignmentUnlinkPreview(
        assignment_id=assignment_id,
        subscriber_id=subscriber_id,
        current_subscription_id=current_subscription_id,
        expected_subscription_id=expected_subscription_id,
        subscription_status=subscription_status,
        subscription_service_address_id=subscription_service_address_id,
        ip_version=ip_version,
        address_id=address_id,
        address=address,
        served_address=served_address,
        served_address_matches_assignment=served_address_matches_assignment,
        live_session_checked=live_session_checked,
        live_session_present=live_session_present,
        is_primary=is_primary,
        service_address_id=service_address_id,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _projection_preview_payload(
    *,
    subscription_id: UUID,
    subscriber_id: UUID | None,
    assignment_id: UUID | None,
    served_address: str | None,
    desired_address: str | None,
    radius_mode: str | None,
    observed_radius_address: str | None,
    active_session_count: int,
    old_address_session_count: int,
    decision: IPv4ServedProjectionDecision,
) -> dict[str, object]:
    return {
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "assignment_id": str(assignment_id) if assignment_id is not None else None,
        "served_address": served_address,
        "desired_address": desired_address,
        "radius_mode": radius_mode,
        "observed_radius_address": observed_radius_address,
        "active_session_count": active_session_count,
        "old_address_session_count": old_address_session_count,
        "decision": decision.value,
    }


def preview_service_ipv4_projection_repair(
    db: Session,
    *,
    subscription_id: UUID,
    assignment_id: UUID,
) -> ServiceIPv4ProjectionPreview:
    """Preview one exact-service served-IP projection repair without writes."""

    from app.services.ip_consistency_audit import _external_ip_state, _norm
    from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES
    from app.services.radius_projection_planner import plan_login_radius_projections

    subscription = db.get(Subscription, subscription_id)
    subscriber_id = subscription.subscriber_id if subscription is not None else None
    served_address = (
        _norm(subscription.ipv4_address) or None if subscription is not None else None
    )
    desired_address: str | None = None
    radius_mode: str | None = None
    observed_radius_address: str | None = None
    active_session_count = 0
    old_address_session_count = 0
    decision = IPv4ServedProjectionDecision.subscription_not_found

    exact_rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == subscription_id,
                IPAssignment.is_active.is_(True),
                IPAssignment.ip_version == IPVersion.ipv4,
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    selected = next(
        (
            (assignment, address)
            for assignment, address in exact_rows
            if assignment.id == assignment_id
        ),
        None,
    )
    if selected is not None:
        desired_address = _norm(selected[1]) or None

    session_addresses = [
        _norm(value) or None
        for value in db.scalars(
            select(RadiusActiveSession.framed_ip_address).where(
                RadiusActiveSession.subscription_id == subscription_id
            )
        ).all()
    ]
    active_session_count = len(session_addresses)
    if served_address is not None:
        old_address_session_count = sum(
            value == served_address for value in session_addresses
        )

    if subscription is None:
        pass
    elif subscription.status is not SubscriptionStatus.active:
        decision = IPv4ServedProjectionDecision.subscription_not_active
    elif len(exact_rows) == 0:
        decision = IPv4ServedProjectionDecision.missing_exact_assignment
    elif len(exact_rows) > 1:
        decision = IPv4ServedProjectionDecision.multiple_exact_assignments
    elif selected is None:
        decision = IPv4ServedProjectionDecision.missing_exact_assignment
    elif selected[0].subscriber_id != subscription.subscriber_id:
        decision = IPv4ServedProjectionDecision.assignment_subscriber_mismatch
    else:
        login = str(subscription.login or "").strip()
        if not login:
            decision = IPv4ServedProjectionDecision.missing_login
        else:
            login_candidates = (
                db.execute(
                    select(Subscription)
                    .options(
                        joinedload(Subscription.subscriber).joinedload(
                            Subscriber.reseller
                        )
                    )
                    .where(
                        Subscription.login == login,
                        Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                    )
                )
                .unique()
                .scalars()
                .all()
            )
            projection = plan_login_radius_projections(db, login_candidates).get(login)
            if projection is None or projection.subscription_id != str(subscription.id):
                decision = IPv4ServedProjectionDecision.shared_login_not_selected
            else:
                radius_mode = projection.plan.mode
                framed, provisioned, errors = _external_ip_state(db, [login])
                observed_radius_address = _norm(framed.get(login)) or None
                if errors:
                    decision = (
                        IPv4ServedProjectionDecision.radius_observation_unavailable
                    )
                elif served_address == desired_address:
                    decision = IPv4ServedProjectionDecision.noop
                elif projection.plan.write_radreply and (
                    login not in provisioned
                    or observed_radius_address != served_address
                ):
                    decision = (
                        IPv4ServedProjectionDecision.radius_projection_not_aligned
                    )
                elif not projection.plan.write_radreply and observed_radius_address:
                    decision = (
                        IPv4ServedProjectionDecision.radius_projection_not_aligned
                    )
                elif any(
                    value not in {served_address, desired_address}
                    for value in session_addresses
                ):
                    decision = IPv4ServedProjectionDecision.session_observation_conflict
                else:
                    decision = IPv4ServedProjectionDecision.ready

    payload = _projection_preview_payload(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        assignment_id=selected[0].id if selected is not None else None,
        served_address=served_address,
        desired_address=desired_address,
        radius_mode=radius_mode,
        observed_radius_address=observed_radius_address,
        active_session_count=active_session_count,
        old_address_session_count=old_address_session_count,
        decision=decision,
    )
    return ServiceIPv4ProjectionPreview(
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
        assignment_id=selected[0].id if selected is not None else None,
        served_address=served_address,
        desired_address=desired_address,
        radius_mode=radius_mode,
        observed_radius_address=observed_radius_address,
        active_session_count=active_session_count,
        old_address_session_count=old_address_session_count,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _prior_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> IPAssignmentOwnershipOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_service_ownership",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    return IPAssignmentOwnershipOutcome(
        preview_fingerprint=prior_fingerprint,
        linked_count=int(metadata.get("linked_count") or 0),
        replayed=True,
    )


def _reconcile(
    db: Session,
    command: ReconcileIPAssignmentOwnershipCommand,
) -> IPAssignmentOwnershipOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _error("missing_idempotency_key", "An idempotency key is required.")
    if not command.assignment_ids:
        _error("empty_cohort", "The reviewed repair cohort is empty.")
    if len(set(command.assignment_ids)) != len(command.assignment_ids):
        _error("duplicate_assignment", "The repair cohort repeats an assignment.")

    prior = _prior_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    initial = preview_ip_assignment_service_ownership(
        db,
        assignment_ids=command.assignment_ids,
    )
    subscription_ids = sorted(
        {
            item.proposed_subscription_id
            for item in initial.items
            if item.proposed_subscription_id is not None
        },
        key=str,
    )
    if subscription_ids:
        list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(subscription_ids))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        )
    subscriber_ids = sorted(
        {
            item.subscriber_id
            for item in initial.items
            if item.subscriber_id is not None
        },
        key=str,
    )
    if subscriber_ids:
        list(
            db.scalars(
                select(Subscriber)
                .where(Subscriber.id.in_(subscriber_ids))
                .order_by(Subscriber.id)
                .with_for_update()
            ).all()
        )
    locked = list(
        db.scalars(
            select(IPAssignment)
            .where(IPAssignment.id.in_(command.assignment_ids))
            .order_by(IPAssignment.id)
            .with_for_update()
        ).all()
    )
    if len(locked) != len(command.assignment_ids):
        _error("assignment_not_found", "A reviewed IP assignment no longer exists.")
    prior = _prior_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    current = preview_ip_assignment_service_ownership(
        db,
        assignment_ids=command.assignment_ids,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _error(
            "stale_preview",
            "IPAM ownership evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if any(not item.repairable for item in current.items):
        _error(
            "unsafe_cohort",
            "The reviewed cohort contains an ambiguous or already-linked assignment.",
        )

    by_id = {assignment.id: assignment for assignment in locked}
    for item in current.items:
        assert item.proposed_subscription_id is not None
        assignment = by_id[item.assignment_id]
        assignment.subscription_id = item.proposed_subscription_id
        stage_audit_event(
            db,
            action=_ITEM_AUDIT_ACTION,
            entity_type="ip_assignment",
            entity_id=str(assignment.id),
            actor_type=AuditActorType.service,
            actor_id=command.context.actor,
            metadata={
                "subscription_id": str(item.proposed_subscription_id),
                "preview_fingerprint": current.fingerprint,
                "reason": command.context.reason,
            },
        )

    stage_audit_event(
        db,
        action=_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_service_ownership",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "preview_fingerprint": current.fingerprint,
            "linked_count": len(current.items),
            "assignment_ids": [str(item.assignment_id) for item in current.items],
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_service_ownership_reconciled,
        {
            "schema_version": 1,
            "preview_fingerprint": current.fingerprint,
            "linked_count": len(current.items),
            "assignment_ids": [str(item.assignment_id) for item in current.items],
        },
        actor=command.context.actor,
    )
    db.flush()
    return IPAssignmentOwnershipOutcome(
        preview_fingerprint=current.fingerprint,
        linked_count=len(current.items),
        replayed=False,
    )


def _prior_lifecycle_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> ServiceIPv4AssignmentRepairOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _LIFECYCLE_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_lifecycle",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _lifecycle_error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    desired_assignment_id = metadata.get("desired_assignment_id")
    return ServiceIPv4AssignmentRepairOutcome(
        subscription_id=UUID(str(metadata["subscription_id"])),
        desired_assignment_id=(
            UUID(str(desired_assignment_id)) if desired_assignment_id else None
        ),
        linked_count=int(metadata.get("linked_count") or 0),
        created_count=int(metadata.get("created_count") or 0),
        deactivated_count=int(metadata.get("deactivated_count") or 0),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _prior_projection_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> ServiceIPv4ProjectionOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _PROJECTION_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_served_projection",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _lifecycle_error(
            "idempotency_conflict",
            "The idempotency key was already used for different projection evidence.",
        )
    event_id = metadata.get("event_id")
    if not event_id:
        _lifecycle_error(
            "event_identity_unavailable",
            "The prior repair predates exact durable-event identity; do not infer it.",
        )
    return ServiceIPv4ProjectionOutcome(
        subscription_id=UUID(str(metadata["subscription_id"])),
        assignment_id=UUID(str(metadata["assignment_id"])),
        event_id=UUID(str(event_id)),
        previous_address=str(metadata["previous_address"]),
        desired_address=str(metadata["desired_address"]),
        observed_active_sessions=int(metadata.get("observed_active_sessions") or 0),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _prefix_length(pool: IpPool | None) -> int | None:
    if pool is None:
        return None
    try:
        return ipaddress.ip_network(pool.cidr, strict=False).prefixlen
    except ValueError:
        return None


def _lock_eligibility_inventory(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            text(
                "LOCK TABLE subscriber_additional_routes, network_devices, "
                "nas_devices, olt_devices, routers, radius_clients IN SHARE MODE"
            )
        )


def _stage_lifecycle_item_audit(
    db: Session,
    *,
    command: RepairServiceIPv4AssignmentCommand,
    assignment: IPAssignment,
    action: str,
    fingerprint: str,
) -> None:
    stage_audit_event(
        db,
        action=_LIFECYCLE_ITEM_AUDIT_ACTION,
        entity_type="ip_assignment",
        entity_id=str(assignment.id),
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "action": action,
            "subscription_id": str(command.subscription_id),
            "ipv4_address_id": (
                str(assignment.ipv4_address_id)
                if assignment.ipv4_address_id is not None
                else None
            ),
            "preview_fingerprint": fingerprint,
            "reason": command.context.reason,
        },
    )


def _repair_service_ipv4_assignment(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _lifecycle_error("missing_idempotency_key", "An idempotency key is required.")
    if len(set(command.deactivate_assignment_ids)) != len(
        command.deactivate_assignment_ids
    ):
        _lifecycle_error(
            "duplicate_assignment",
            "The deactivation cohort repeats an assignment.",
        )

    prior = _prior_lifecycle_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _lifecycle_error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == subscription.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        _lifecycle_error(
            "subscriber_not_found",
            "The reviewed subscription no longer has a subscriber.",
        )
    desired_address = None
    desired_pool = None
    if command.desired_address_id is not None:
        desired_address = db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == command.desired_address_id)
            .with_for_update()
        )
        if desired_address is not None and desired_address.pool_id is not None:
            desired_pool = db.scalar(
                select(IpPool)
                .where(IpPool.id == desired_address.pool_id)
                .with_for_update()
            )
    _lock_eligibility_inventory(db)
    assignment_lock_filter = IPAssignment.subscriber_id == subscriber.id
    if command.desired_address_id is not None:
        assignment_lock_filter = or_(
            assignment_lock_filter,
            IPAssignment.ipv4_address_id == command.desired_address_id,
        )
    locked_assignments = list(
        db.scalars(
            select(IPAssignment)
            .where(assignment_lock_filter)
            .order_by(IPAssignment.id)
            .with_for_update()
        ).all()
    )

    if command.desired_address_id is None:
        # Release: lock the freed address rows too, so the allocator
        # (skip_locked) waits and the re-preview below sees a stable address.
        released_address_ids = [
            row.ipv4_address_id
            for row in locked_assignments
            if row.is_active
            and row.ip_version == IPVersion.ipv4
            and row.ipv4_address_id is not None
        ]
        if released_address_ids:
            try:
                db.scalars(
                    select(IPv4Address)
                    .where(IPv4Address.id.in_(released_address_ids))
                    .order_by(IPv4Address.id)
                    .with_for_update(nowait=True)
                ).all()
            except OperationalError as exc:
                if "55P03" not in str(getattr(exc.orig, "sqlstate", "")) and (
                    "could not obtain lock" not in str(exc.orig).lower()
                ):
                    raise
                _lifecycle_error(
                    "address_lock_unavailable",
                    "A released address is locked by another writer; retry later.",
                )

    prior = _prior_lifecycle_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    current = preview_service_ipv4_assignment_repair(
        db,
        subscription_id=command.subscription_id,
        desired_address_id=command.desired_address_id,
        deactivate_assignment_ids=command.deactivate_assignment_ids,
        retention_cutoff=command.retention_cutoff,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _lifecycle_error(
            "stale_preview",
            "IPAM lifecycle evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _lifecycle_error(
            "unsafe_repair",
            "The reviewed IPAM lifecycle repair is no longer safe to apply.",
            decision=current.decision.value,
        )

    active_by_id = {
        assignment.id: assignment
        for assignment in locked_assignments
        if assignment.is_active
    }
    deactivated_count = 0
    for assignment_id in current.deactivate_assignment_ids:
        assignment = active_by_id[assignment_id]
        assignment.is_active = False
        deactivated_count += 1
        _stage_lifecycle_item_audit(
            db,
            command=command,
            assignment=assignment,
            action="deactivated",
            fingerprint=current.fingerprint,
        )
    if current.decision is IPv4AssignmentRepairDecision.ready_release_retention_expired:
        # A released hold must not stay served: on reactivation RADIUS would
        # re-pin the stale column and recreate the collision. Clear it only
        # when it still names a released address.
        released = {
            item.address
            for item in current.active_assignments
            if item.assignment_id in current.deactivate_assignment_ids
        }
        served = _normalized_ip(subscription.ipv4_address).removesuffix("/32")
        if served and served in released:
            _project_served_ipv4(subscription, None)
    if deactivated_count:
        db.flush()

    desired_assignment: IPAssignment | None = None
    linked_count = 0
    created_count = 0
    if command.desired_address_id is not None:
        desired_assignment = next(
            (
                assignment
                for assignment in locked_assignments
                if assignment.is_active
                and assignment.ip_version == IPVersion.ipv4
                and assignment.ipv4_address_id == command.desired_address_id
            ),
            None,
        )
        if desired_assignment is None:
            assert desired_address is not None
            desired_assignment = IPAssignment(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                service_address_id=subscription.service_address_id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=desired_address.id,
                prefix_length=_prefix_length(desired_pool),
                gateway=desired_pool.gateway if desired_pool is not None else None,
                dns_primary=(
                    desired_pool.dns_primary if desired_pool is not None else None
                ),
                dns_secondary=(
                    desired_pool.dns_secondary if desired_pool is not None else None
                ),
                is_active=True,
            )
            db.add(desired_assignment)
            db.flush()
            mark_primary_ipv4_assignment(
                db,
                subscription_id=subscription.id,
                ipv4_address_id=desired_assignment.ipv4_address_id,
            )
            created_count = 1
            _stage_lifecycle_item_audit(
                db,
                command=command,
                assignment=desired_assignment,
                action="created",
                fingerprint=current.fingerprint,
            )
        elif desired_assignment.subscription_id is None:
            desired_assignment.subscription_id = subscription.id
            desired_assignment.service_address_id = subscription.service_address_id
            db.flush()
            mark_primary_ipv4_assignment(
                db,
                subscription_id=subscription.id,
                ipv4_address_id=desired_assignment.ipv4_address_id,
            )
            linked_count = 1
            _stage_lifecycle_item_audit(
                db,
                command=command,
                assignment=desired_assignment,
                action="linked",
                fingerprint=current.fingerprint,
            )

    stage_audit_event(
        db,
        action=_LIFECYCLE_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_lifecycle",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "subscription_id": str(subscription.id),
            "desired_assignment_id": (
                str(desired_assignment.id) if desired_assignment is not None else None
            ),
            "desired_address_id": (
                str(command.desired_address_id)
                if command.desired_address_id is not None
                else None
            ),
            "deactivate_assignment_ids": [
                str(value) for value in current.deactivate_assignment_ids
            ],
            "decision": current.decision.value,
            "preview_fingerprint": current.fingerprint,
            "linked_count": linked_count,
            "created_count": created_count,
            "deactivated_count": deactivated_count,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_lifecycle_repaired,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "desired_assignment_id": (
                str(desired_assignment.id) if desired_assignment is not None else None
            ),
            "deactivated_assignment_ids": [
                str(value) for value in current.deactivate_assignment_ids
            ],
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
    )
    db.flush()
    return ServiceIPv4AssignmentRepairOutcome(
        subscription_id=subscription.id,
        desired_assignment_id=(
            desired_assignment.id if desired_assignment is not None else None
        ),
        linked_count=linked_count,
        created_count=created_count,
        deactivated_count=deactivated_count,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def _prior_ipv6_link_outcome(
    db: Session,
    *,
    subscription_id: UUID,
    idempotency_key: str,
    preview_fingerprint: str,
) -> ServiceIPv6AssignmentLinkOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _IPV6_LIFECYCLE_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_ipv6_lifecycle",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _lifecycle_error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    desired_assignment_id = metadata.get("desired_assignment_id")
    # The preview fingerprint binds subscription_id (it is in the preview
    # payload), so a fingerprint match ties this prior batch to the caller's
    # subscription. Take it from the command, not audit metadata: metadata
    # is provenance only (ADR 0007 invariant 4), never identity authority.
    return ServiceIPv6AssignmentLinkOutcome(
        subscription_id=subscription_id,
        desired_assignment_id=(
            UUID(str(desired_assignment_id)) if desired_assignment_id else None
        ),
        linked_count=int(metadata.get("linked_count") or 0),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _link_service_ipv6_assignment(
    db: Session,
    command: LinkServiceIPv6AssignmentCommand,
) -> ServiceIPv6AssignmentLinkOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _lifecycle_error("missing_idempotency_key", "An idempotency key is required.")

    prior = _prior_ipv6_link_outcome(
        db,
        subscription_id=command.subscription_id,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _lifecycle_error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == subscription.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        _lifecycle_error(
            "subscriber_not_found",
            "The reviewed subscription no longer has a subscriber.",
        )
    desired_address = db.scalar(
        select(IPv6Address)
        .where(IPv6Address.id == command.desired_address_id)
        .with_for_update()
    )
    if desired_address is not None and desired_address.pool_id is not None:
        db.scalar(
            select(IpPool).where(IpPool.id == desired_address.pool_id).with_for_update()
        )
    locked_assignments = list(
        db.scalars(
            select(IPAssignment)
            .where(
                or_(
                    IPAssignment.subscriber_id == subscriber.id,
                    IPAssignment.ipv6_address_id == command.desired_address_id,
                )
            )
            .order_by(IPAssignment.id)
            .with_for_update()
        ).all()
    )

    prior = _prior_ipv6_link_outcome(
        db,
        subscription_id=command.subscription_id,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    current = preview_service_ipv6_assignment_link(
        db,
        subscription_id=command.subscription_id,
        desired_address_id=command.desired_address_id,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _lifecycle_error(
            "stale_preview",
            "IPAM lifecycle evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _lifecycle_error(
            "unsafe_repair",
            "The reviewed IPAM lifecycle repair is no longer safe to apply.",
            decision=current.decision.value,
        )

    desired_assignment = next(
        (
            assignment
            for assignment in locked_assignments
            if assignment.is_active
            and assignment.ip_version == IPVersion.ipv6
            and assignment.ipv6_address_id == command.desired_address_id
        ),
        None,
    )
    if desired_assignment is None:
        _lifecycle_error(
            "assignment_not_found",
            "The reviewed IP assignment no longer exists.",
        )
    # Mirror the IPv4 link branch minus the primary marker, which IPv6
    # assignments do not carry.
    desired_assignment.subscription_id = subscription.id
    desired_assignment.service_address_id = subscription.service_address_id
    db.flush()
    stage_audit_event(
        db,
        action=_LIFECYCLE_ITEM_AUDIT_ACTION,
        entity_type="ip_assignment",
        entity_id=str(desired_assignment.id),
        actor=AuditActor.service(command.context.actor),
        metadata={
            "action": "linked",
            "subscription_id": str(command.subscription_id),
            "ipv6_address_id": str(command.desired_address_id),
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
        },
    )
    stage_audit_event(
        db,
        action=_IPV6_LIFECYCLE_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_ipv6_lifecycle",
        entity_id=idempotency_key,
        actor=AuditActor.service(command.context.actor),
        metadata={
            "subscription_id": str(subscription.id),
            "desired_assignment_id": str(desired_assignment.id),
            "desired_address_id": str(command.desired_address_id),
            "deactivate_assignment_ids": [],
            "decision": current.decision.value,
            "preview_fingerprint": current.fingerprint,
            "linked_count": 1,
            "created_count": 0,
            "deactivated_count": 0,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_lifecycle_repaired,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "desired_assignment_id": str(desired_assignment.id),
            "deactivated_assignment_ids": [],
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
    )
    db.flush()
    return ServiceIPv6AssignmentLinkOutcome(
        subscription_id=subscription.id,
        desired_assignment_id=desired_assignment.id,
        linked_count=1,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def _prior_unlink_outcome(
    db: Session,
    *,
    idempotency_key: str,
    preview_fingerprint: str,
) -> IPAssignmentServiceUnlinkOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _UNLINK_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_service_ownership_unlink",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, preview_fingerprint):
        _error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    previous_subscription_id = metadata.get("previous_subscription_id")
    return IPAssignmentServiceUnlinkOutcome(
        assignment_id=UUID(str(metadata["assignment_id"])),
        previous_subscription_id=(
            UUID(str(previous_subscription_id)) if previous_subscription_id else None
        ),
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _unlink_ip_assignment_service_ownership(
    db: Session,
    command: UnlinkIPAssignmentServiceOwnershipCommand,
) -> IPAssignmentServiceUnlinkOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _error("missing_idempotency_key", "An idempotency key is required.")

    prior = _prior_unlink_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.expected_subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    assignment = db.scalar(
        select(IPAssignment)
        .where(IPAssignment.id == command.assignment_id)
        .with_for_update()
    )
    if assignment is None:
        _error("assignment_not_found", "A reviewed IP assignment no longer exists.")
    if assignment.ipv6_address_id is not None:
        db.scalar(
            select(IPv6Address)
            .where(IPv6Address.id == assignment.ipv6_address_id)
            .with_for_update()
        )
    elif assignment.ipv4_address_id is not None:
        db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == assignment.ipv4_address_id)
            .with_for_update()
        )

    prior = _prior_unlink_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    current = preview_ip_assignment_service_unlink(
        db,
        assignment_id=command.assignment_id,
        expected_subscription_id=command.expected_subscription_id,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _error(
            "stale_preview",
            "IPAM ownership evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _error(
            "unsafe_cohort",
            "The reviewed assignment is no longer safe to unlink.",
            decision=current.decision.value,
        )

    previous_subscription_id = assignment.subscription_id
    # Rollback only: clear the service link and the IPv4 primary marker the
    # link set. Never deactivate or release the address, never re-promote a
    # sibling (its pre-link primary state is unknowable; consumers fall back
    # to a sole assignment), and never touch a served column.
    assignment.subscription_id = None
    if assignment.is_primary:
        assignment.is_primary = False
    if (
        assignment.service_address_id is not None
        and assignment.service_address_id == subscription.service_address_id
    ):
        assignment.service_address_id = None
    stage_audit_event(
        db,
        action=_UNLINK_ITEM_AUDIT_ACTION,
        entity_type="ip_assignment",
        entity_id=str(assignment.id),
        actor=AuditActor.service(command.context.actor),
        metadata={
            "previous_subscription_id": (
                str(previous_subscription_id)
                if previous_subscription_id is not None
                else None
            ),
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
        },
    )
    stage_audit_event(
        db,
        action=_UNLINK_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_service_ownership_unlink",
        entity_id=idempotency_key,
        actor=AuditActor.service(command.context.actor),
        metadata={
            "assignment_id": str(assignment.id),
            "previous_subscription_id": (
                str(previous_subscription_id)
                if previous_subscription_id is not None
                else None
            ),
            "preview_fingerprint": current.fingerprint,
            "unlinked_count": 1,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_service_ownership_unlinked,
        {
            "schema_version": 1,
            "assignment_id": str(assignment.id),
            "previous_subscription_id": (
                str(previous_subscription_id)
                if previous_subscription_id is not None
                else None
            ),
            "ip_version": current.ip_version,
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
    )
    db.flush()
    return IPAssignmentServiceUnlinkOutcome(
        assignment_id=assignment.id,
        previous_subscription_id=previous_subscription_id,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def _project_served_ipv4(subscription: Subscription, value: str | None) -> None:
    """THE one write of the served-IPv4 column (projection and retention release)."""

    from app.services.connectivity_reconciler import (
        note_connectivity_write,
        reconciler_write_scope,
    )

    with reconciler_write_scope():
        subscription.ipv4_address = value
        note_connectivity_write(
            "subscription.ipv4_address",
            "ip_assignment_lifecycle",
        )


def _repair_service_ipv4_projection(
    db: Session,
    command: RepairServiceIPv4ProjectionCommand,
) -> ServiceIPv4ProjectionOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _lifecycle_error("missing_idempotency_key", "An idempotency key is required.")
    prior = _prior_projection_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    )
    if subscription is None:
        _lifecycle_error(
            "subscription_not_found",
            "The reviewed subscription no longer exists.",
        )
    assignment = db.scalar(
        select(IPAssignment)
        .where(IPAssignment.id == command.assignment_id)
        .with_for_update()
    )
    if assignment is None:
        _lifecycle_error(
            "assignment_not_found",
            "The reviewed IP assignment no longer exists.",
        )
    if assignment.ipv4_address_id is not None:
        db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == assignment.ipv4_address_id)
            .with_for_update()
        )

    prior = _prior_projection_outcome(
        db,
        idempotency_key=idempotency_key,
        preview_fingerprint=command.preview_fingerprint,
    )
    if prior is not None:
        return prior
    current = preview_service_ipv4_projection_repair(
        db,
        subscription_id=command.subscription_id,
        assignment_id=command.assignment_id,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _lifecycle_error(
            "stale_preview",
            "IPv4 projection evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _lifecycle_error(
            "unsafe_projection_repair",
            "The reviewed IPv4 projection repair is no longer safe to apply.",
            decision=current.decision.value,
        )
    if (
        current.assignment_id is None
        or current.served_address is None
        or current.desired_address is None
    ):
        _lifecycle_error(
            "incomplete_projection_evidence",
            "The reviewed IPv4 projection evidence is incomplete.",
        )

    _project_served_ipv4(subscription, current.desired_address)

    event = emit_event(
        db,
        EventType.ip_assignment_served_projection_repaired,
        {
            "schema_version": 1,
            "subscription_id": str(subscription.id),
            "assignment_id": str(current.assignment_id),
            "previous_address": current.served_address,
            "desired_address": current.desired_address,
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    stage_audit_event(
        db,
        action=_PROJECTION_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_served_projection",
        entity_id=idempotency_key,
        actor_type=AuditActorType.service,
        actor_id=command.context.actor,
        metadata={
            "subscription_id": str(subscription.id),
            "assignment_id": str(current.assignment_id),
            "event_id": str(event.event_id),
            "previous_address": current.served_address,
            "desired_address": current.desired_address,
            "radius_mode": current.radius_mode,
            "observed_active_sessions": current.active_session_count,
            "old_address_sessions": current.old_address_session_count,
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
        },
    )
    db.flush()
    return ServiceIPv4ProjectionOutcome(
        subscription_id=subscription.id,
        assignment_id=current.assignment_id,
        event_id=event.event_id,
        previous_address=current.served_address,
        desired_address=current.desired_address,
        observed_active_sessions=current.active_session_count,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def reconcile_ip_assignment_service_ownership(
    db: Session,
    command: ReconcileIPAssignmentOwnershipCommand,
) -> IPAssignmentOwnershipOutcome:
    """Apply one exact, fingerprint-bound service-ownership repair."""

    return execute_owner_command(
        db,
        definition=_OWNERSHIP_COMMAND,
        context=command.context,
        operation=lambda: _reconcile(db, command),
    )


def repair_service_ipv4_assignment(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    """Apply one exact, fingerprint-bound service IPv4 assignment repair."""

    return execute_owner_command(
        db,
        definition=_LIFECYCLE_COMMAND,
        context=command.context,
        operation=lambda: _repair_service_ipv4_assignment(db, command),
    )


def link_service_ipv6_assignment(
    db: Session,
    command: LinkServiceIPv6AssignmentCommand,
) -> ServiceIPv6AssignmentLinkOutcome:
    """Apply one exact, fingerprint-bound service IPv6 assignment link."""

    return execute_owner_command(
        db,
        definition=_IPV6_LINK_COMMAND,
        context=command.context,
        operation=lambda: _link_service_ipv6_assignment(db, command),
    )


def unlink_ip_assignment_service_ownership(
    db: Session,
    command: UnlinkIPAssignmentServiceOwnershipCommand,
) -> IPAssignmentServiceUnlinkOutcome:
    """Undo one exact, fingerprint-bound assignment service link."""

    return execute_owner_command(
        db,
        definition=_UNLINK_COMMAND,
        context=command.context,
        operation=lambda: _unlink_ip_assignment_service_ownership(db, command),
    )


def apply_service_ipv4_assignment_participant(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    """Apply reviewed IPv4 ledger work inside a coordinating owner command.

    The caller owns the root transaction. This participant remains flush-only
    and preserves ``network.ip_assignment_lifecycle`` as the canonical writer.
    Architecture tests restrict the call site to the reviewed service-access
    move coordinator.
    """

    return _repair_service_ipv4_assignment(db, command)


def _replace_service_ipv4(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    assignment_outcome = apply_service_ipv4_assignment_participant(db, command)
    if assignment_outcome.desired_assignment_id is None:
        _lifecycle_error(
            "incomplete_outcome",
            "IPv4 assignment owner did not return a desired assignment.",
        )
    # The assignment change is flushed, so this preview sees the new IPAM
    # state. Any refusal raises and rolls back the assignment change too.
    projection_preview = preview_service_ipv4_projection_repair(
        db,
        subscription_id=command.subscription_id,
        assignment_id=assignment_outcome.desired_assignment_id,
    )
    if projection_preview.decision is IPv4ServedProjectionDecision.noop:
        return assignment_outcome
    if not projection_preview.applicable:
        _lifecycle_error(
            "unsafe_projection_repair",
            "The served IPv4 projection cannot be completed in this "
            "replacement; IPAM was not changed.",
            decision=projection_preview.decision.value,
        )
    base_key = str(command.context.idempotency_key or "").strip()
    apply_service_ipv4_projection_participant(
        db,
        RepairServiceIPv4ProjectionCommand(
            context=CommandContext(
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
                causation_id=command.context.causation_id,
                actor=command.context.actor,
                scope=command.context.scope,
                reason=command.context.reason,
                idempotency_key=f"{base_key}:ipv4-projection",
            ),
            subscription_id=command.subscription_id,
            assignment_id=assignment_outcome.desired_assignment_id,
            preview_fingerprint=projection_preview.fingerprint,
        ),
    )
    return assignment_outcome


def replace_service_ipv4(
    db: Session,
    command: RepairServiceIPv4AssignmentCommand,
) -> ServiceIPv4AssignmentRepairOutcome:
    """Move IPAM and the served projection in ONE transaction, or neither.

    Tactical stopgap (pre-Starter dotmac-ipam): the assignment participant and
    the projection participant run in a single owner command, so a projection
    refusal rolls the IPAM move back instead of leaving the old address looking
    free while the customer is still served on it.
    """

    return execute_owner_command(
        db,
        definition=_REPLACE_COMMAND,
        context=command.context,
        operation=lambda: _replace_service_ipv4(db, command),
    )


def repair_service_ipv4_projection(
    db: Session,
    command: RepairServiceIPv4ProjectionCommand,
) -> ServiceIPv4ProjectionOutcome:
    """Converge one exact-service served-IP projection after reviewed preview."""

    return execute_owner_command(
        db,
        definition=_PROJECTION_COMMAND,
        context=command.context,
        operation=lambda: _repair_service_ipv4_projection(db, command),
    )


def apply_service_ipv4_projection_participant(
    db: Session,
    command: RepairServiceIPv4ProjectionCommand,
) -> ServiceIPv4ProjectionOutcome:
    """Apply the served-IP projection inside a coordinating owner command."""

    return _repair_service_ipv4_projection(db, command)


@dataclass(frozen=True, slots=True)
class ReleaseSubscriberIPAssignmentsCommand:
    context: CommandContext = field(repr=False)
    subscriber_id: UUID
    actor: AuditActor = field(repr=False)


@dataclass(frozen=True, slots=True)
class SubscriberIPAssignmentReleaseOutcome:
    released_assignment_ids: tuple[UUID, ...]


def stage_subscriber_ip_assignment_release(
    db: Session, *, command: ReleaseSubscriberIPAssignmentsCommand
) -> SubscriberIPAssignmentReleaseOutcome:
    """Release active IPv4/IPv6 rows within exact customer hard deletion.

    The coordinator must first lock and refresh the exact Subscriber. Child
    locks never wait while holding that parent; a lock error aborts the entire
    root command. History stays attached until the migrated SET NULL FK runs.
    No projection, transport, repair, audit, or transaction is owned here.
    """
    if (
        not isinstance(command, ReleaseSubscriberIPAssignmentsCommand)
        or not isinstance(command.subscriber_id, UUID)
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor
        != f"{command.actor.actor_type.value}:{command.actor.actor_id}"
        or not owner_command_active(db, owner="customer.account_deletion")
        or current_command_definition(db)
        != OwnerCommandDefinition(
            owner="customer.account_deletion",
            concern="customer account hard deletion",
            name="delete_customer_account",
        )
        or current_command_context(db) != command.context
    ):
        raise IPv4AssignmentLifecycleError(
            code=f"{_OWNER}.invalid_customer_deletion_participant",
            message="Exact trusted customer deletion command required.",
        )
    with db.no_autoflush:
        assignments = tuple(
            db.scalars(
                select(IPAssignment)
                .where(IPAssignment.subscriber_id == command.subscriber_id)
                .order_by(IPAssignment.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        )
    released: list[UUID] = []
    for assignment in assignments:
        if assignment.is_active:
            assignment.is_active = False
            released.append(assignment.id)
    db.flush()
    return SubscriberIPAssignmentReleaseOutcome(tuple(released))


# ---------------------------------------------------------------------------
# Reviewed release of one unbound IPv4 assignment (W0 N9 named exception)
# ---------------------------------------------------------------------------
#
# N9 is LINK-DON'T-RELEASE: an active assignment that no service owns is linked
# to its service, never freed. This command is the narrow, named exception for
# rows that cannot be linked. The caller must NAME the eligibility class it
# invokes, and the preview refuses unless the evidence proves that class. There
# is no class for "ambiguous" or "no column match on a retaining service": those
# stay held until Michael rules per item.

_UNBOUND_RELEASE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="release_unbound_ip_assignment",
)
_UNBOUND_RELEASE_ITEM_AUDIT_ACTION = "network.ip_assignment.unbound_released"
_UNBOUND_RELEASE_BATCH_AUDIT_ACTION = "network.ip_assignment.unbound_released_batch"


class UnboundIPv4ReleaseClass(StrEnum):
    #: R6 / C8: the owning subscriber has no non-terminal service at all (every
    #: service canceled/expired, or none), and the address is an ordinary
    #: customer address.
    terminal_only_subscriber = "terminal_only_subscriber"
    #: R6 / C10: the address cannot be served to a customer (network or
    #: broadcast address, or a device/infrastructure/reserved address) AND stays
    #: protected from re-allocation after the release.
    address_not_serviceable = "address_not_serviceable"
    #: R3 / C4: the subscriber keeps a served IP elsewhere; no non-terminal
    #: service of the subscriber carries this address in its served column.
    served_elsewhere_no_sibling = "served_elsewhere_no_sibling"


class UnboundIPv4ReleaseDecision(StrEnum):
    ready_release = "ready_release"
    assignment_not_found = "assignment_not_found"
    assignment_not_active = "assignment_not_active"
    assignment_not_ipv4 = "assignment_not_ipv4"
    assignment_already_bound = "assignment_already_bound"
    assignment_is_primary = "assignment_is_primary"
    subscriber_not_found = "subscriber_not_found"
    address_not_found = "address_not_found"
    address_in_live_use = "address_in_live_use"
    # terminal_only_subscriber refusals
    subscriber_has_non_terminal_service = "subscriber_has_non_terminal_service"
    address_not_releasable = "address_not_releasable"
    address_in_routed_block = "address_in_routed_block"
    address_is_device_host = "address_is_device_host"
    # address_not_serviceable refusals
    address_is_serviceable = "address_is_serviceable"
    release_would_free_unprotected_address = "release_would_free_unprotected_address"
    # served_elsewhere_no_sibling refusals
    linkable_sibling_exists = "linkable_sibling_exists"
    ambiguous_sibling_column_match = "ambiguous_sibling_column_match"
    service_without_exact_assignment = "service_without_exact_assignment"
    no_non_terminal_service = "no_non_terminal_service"


@dataclass(frozen=True, slots=True)
class UnboundIPv4ServiceEvidence:
    subscription_id: UUID
    status: str
    column_matches_address: bool
    has_exact_active_ipv4: bool


@dataclass(frozen=True, slots=True)
class UnboundIPv4ReleasePreview:
    assignment_id: UUID
    eligibility_class: UnboundIPv4ReleaseClass
    subscriber_id: UUID | None
    address_id: UUID | None
    address: str | None
    is_active: bool
    is_primary: bool
    current_subscription_id: UUID | None
    services: tuple[UnboundIPv4ServiceEvidence, ...]
    live_session: bool
    column_holder_subscription_ids: tuple[UUID, ...]
    address_is_network_or_broadcast: bool
    address_is_protected: bool
    address_is_device_host: bool
    address_in_routed_block: bool
    decision: UnboundIPv4ReleaseDecision
    fingerprint: str

    @property
    def applicable(self) -> bool:
        return self.decision is UnboundIPv4ReleaseDecision.ready_release


@dataclass(frozen=True, slots=True)
class ReleaseUnboundIPAssignmentCommand:
    context: CommandContext
    assignment_id: UUID
    eligibility_class: UnboundIPv4ReleaseClass
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class UnboundIPAssignmentReleaseOutcome:
    assignment_id: UUID
    eligibility_class: UnboundIPv4ReleaseClass
    released_count: int
    preview_fingerprint: str
    replayed: bool


def _address_is_network_or_broadcast(address: str, pool: IpPool | None) -> bool:
    if pool is None:
        return False
    try:
        network = ipaddress.ip_network(str(pool.cidr), strict=False)
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen >= 31:
        return False
    return parsed in {network.network_address, network.broadcast_address}


def _address_stays_protected(address: IPv4Address) -> bool:
    """True when the allocator keeps refusing this row after a release.

    The allocator excludes only ``is_reserved`` and ONT-linked rows, so a
    management/device address without either flag would become assignable to a
    customer once its stale assignment is released.
    """

    return bool(address.is_reserved) or address.ont_unit_id is not None


def preview_unbound_ip_assignment_release(
    db: Session,
    *,
    assignment_id: UUID,
    eligibility_class: UnboundIPv4ReleaseClass,
) -> UnboundIPv4ReleasePreview:
    """Read-only plan to release ONE active IPv4 assignment no service owns.

    Refuses unless the evidence proves the named ``eligibility_class``. The
    fingerprint binds the class, the subscriber's service set, the live-use
    evidence and the address class, so apply refuses on any drift.
    """

    assignment = db.get(IPAssignment, assignment_id)
    subscriber_id = assignment.subscriber_id if assignment is not None else None
    address_row: IPv4Address | None = None
    pool: IpPool | None = None
    address: str | None = None
    services: list[UnboundIPv4ServiceEvidence] = []
    live_session = False
    holders: tuple[UUID, ...] = ()
    network_or_broadcast = False
    protected = False
    device_host = False
    routed = False
    decision = UnboundIPv4ReleaseDecision.ready_release

    if assignment is None:
        decision = UnboundIPv4ReleaseDecision.assignment_not_found
    elif not assignment.is_active:
        decision = UnboundIPv4ReleaseDecision.assignment_not_active
    elif assignment.ip_version != IPVersion.ipv4 or assignment.ipv4_address_id is None:
        decision = UnboundIPv4ReleaseDecision.assignment_not_ipv4
    elif assignment.subscription_id is not None:
        decision = UnboundIPv4ReleaseDecision.assignment_already_bound
    elif assignment.is_primary:
        decision = UnboundIPv4ReleaseDecision.assignment_is_primary
    elif subscriber_id is None or db.get(Subscriber, subscriber_id) is None:
        decision = UnboundIPv4ReleaseDecision.subscriber_not_found
    else:
        address_row = db.get(IPv4Address, assignment.ipv4_address_id)
        if address_row is None:
            decision = UnboundIPv4ReleaseDecision.address_not_found

    if (
        assignment is not None
        and address_row is not None
        and decision is UnboundIPv4ReleaseDecision.ready_release
        and subscriber_id is not None
    ):
        address = _normalized_ip(address_row.address).removesuffix("/32")
        candidates = _address_candidates({address})
        pool = db.get(IpPool, address_row.pool_id) if address_row.pool_id else None
        network_or_broadcast = _address_is_network_or_broadcast(address, pool)
        protected = _address_stays_protected(address_row)
        device_host = _target_address_is_device_host(db, address)
        routed = _address_is_in_active_routed_block(db, address)

        subscriptions = db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .order_by(Subscription.id)
        ).all()
        exact_service_ids = {
            value
            for value in db.scalars(
                select(IPAssignment.subscription_id).where(
                    IPAssignment.is_active.is_(True),
                    IPAssignment.ip_version == IPVersion.ipv4,
                    IPAssignment.subscription_id.in_([s.id for s in subscriptions]),
                )
            )
            if value is not None
        }
        for subscription in subscriptions:
            if subscription.status in _TERMINAL_STATUSES:
                continue
            services.append(
                UnboundIPv4ServiceEvidence(
                    subscription_id=subscription.id,
                    status=subscription.status.value,
                    column_matches_address=(
                        str(subscription.ipv4_address or "").strip() in candidates
                    ),
                    has_exact_active_ipv4=subscription.id in exact_service_ids,
                )
            )
        # Nil subscription id: this assignment has no service, so every
        # non-terminal column holder is a "sibling".
        live = find_ipv4_live_use(db, subscription_id=UUID(int=0), addresses={address})
        live_session = live.live_session
        holders = live.sibling_subscription_ids
        own_matches = [s for s in services if s.column_matches_address]

        if live_session:
            decision = UnboundIPv4ReleaseDecision.address_in_live_use
        elif eligibility_class is UnboundIPv4ReleaseClass.served_elsewhere_no_sibling:
            if not services:
                decision = UnboundIPv4ReleaseDecision.no_non_terminal_service
            elif len(own_matches) == 1:
                decision = UnboundIPv4ReleaseDecision.linkable_sibling_exists
            elif len(own_matches) > 1:
                decision = UnboundIPv4ReleaseDecision.ambiguous_sibling_column_match
            elif holders:
                decision = UnboundIPv4ReleaseDecision.address_in_live_use
            elif not all(s.has_exact_active_ipv4 for s in services):
                decision = UnboundIPv4ReleaseDecision.service_without_exact_assignment
            elif device_host:
                decision = UnboundIPv4ReleaseDecision.address_is_device_host
            elif routed:
                decision = UnboundIPv4ReleaseDecision.address_in_routed_block
            elif not _target_address_is_serviceable(address_row, pool):
                decision = UnboundIPv4ReleaseDecision.address_not_releasable
        elif holders:
            decision = UnboundIPv4ReleaseDecision.address_in_live_use
        elif eligibility_class is UnboundIPv4ReleaseClass.terminal_only_subscriber:
            if services:
                decision = (
                    UnboundIPv4ReleaseDecision.subscriber_has_non_terminal_service
                )
            elif not _target_address_is_serviceable(address_row, pool):
                decision = UnboundIPv4ReleaseDecision.address_not_releasable
            elif network_or_broadcast:
                decision = UnboundIPv4ReleaseDecision.address_not_releasable
            elif device_host:
                decision = UnboundIPv4ReleaseDecision.address_is_device_host
            elif routed:
                decision = UnboundIPv4ReleaseDecision.address_in_routed_block
        else:  # address_not_serviceable
            unserviceable = (
                network_or_broadcast
                or device_host
                or routed
                or not _target_address_is_serviceable(address_row, pool)
            )
            if not unserviceable:
                decision = UnboundIPv4ReleaseDecision.address_is_serviceable
            elif not (protected or network_or_broadcast):
                decision = (
                    UnboundIPv4ReleaseDecision.release_would_free_unprotected_address
                )

    payload: dict[str, object] = {
        "assignment_id": str(assignment_id),
        "eligibility_class": eligibility_class.value,
        "subscriber_id": str(subscriber_id) if subscriber_id is not None else None,
        "address_id": (str(address_row.id) if address_row is not None else None),
        "address": address,
        "is_active": bool(assignment.is_active) if assignment is not None else False,
        "is_primary": bool(assignment.is_primary) if assignment is not None else False,
        "current_subscription_id": (
            str(assignment.subscription_id)
            if assignment is not None and assignment.subscription_id is not None
            else None
        ),
        "services": [
            {
                "subscription_id": str(item.subscription_id),
                "status": item.status,
                "column_matches_address": item.column_matches_address,
                "has_exact_active_ipv4": item.has_exact_active_ipv4,
            }
            for item in services
        ],
        "live_session": live_session,
        "column_holder_subscription_ids": [str(value) for value in holders],
        "address_is_network_or_broadcast": network_or_broadcast,
        "address_is_protected": protected,
        "address_is_device_host": device_host,
        "address_in_routed_block": routed,
        "decision": decision.value,
    }
    return UnboundIPv4ReleasePreview(
        assignment_id=assignment_id,
        eligibility_class=eligibility_class,
        subscriber_id=subscriber_id,
        address_id=address_row.id if address_row is not None else None,
        address=address,
        is_active=bool(assignment.is_active) if assignment is not None else False,
        is_primary=bool(assignment.is_primary) if assignment is not None else False,
        current_subscription_id=(
            assignment.subscription_id if assignment is not None else None
        ),
        services=tuple(services),
        live_session=live_session,
        column_holder_subscription_ids=holders,
        address_is_network_or_broadcast=network_or_broadcast,
        address_is_protected=protected,
        address_is_device_host=device_host,
        address_in_routed_block=routed,
        decision=decision,
        fingerprint=_repair_fingerprint(payload),
    )


def _prior_unbound_release_outcome(
    db: Session,
    *,
    command: ReleaseUnboundIPAssignmentCommand,
    idempotency_key: str,
) -> UnboundIPAssignmentReleaseOutcome | None:
    prior = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == _UNBOUND_RELEASE_BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "ip_assignment_unbound_release",
            AuditEvent.entity_id == idempotency_key,
        )
    )
    if prior is None:
        return None
    metadata = prior.metadata_ if isinstance(prior.metadata_, dict) else {}
    prior_fingerprint = str(metadata.get("preview_fingerprint") or "")
    if not secrets.compare_digest(prior_fingerprint, command.preview_fingerprint):
        _error(
            "idempotency_conflict",
            "The idempotency key was already used for different IPAM evidence.",
        )
    return UnboundIPAssignmentReleaseOutcome(
        assignment_id=command.assignment_id,
        eligibility_class=command.eligibility_class,
        released_count=0,
        preview_fingerprint=prior_fingerprint,
        replayed=True,
    )


def _release_unbound_ip_assignment(
    db: Session,
    command: ReleaseUnboundIPAssignmentCommand,
) -> UnboundIPAssignmentReleaseOutcome:
    idempotency_key = (command.context.idempotency_key or "").strip()
    if not idempotency_key:
        _error("missing_idempotency_key", "An idempotency key is required.")
    prior = _prior_unbound_release_outcome(
        db, command=command, idempotency_key=idempotency_key
    )
    if prior is not None:
        return prior

    assignment = db.scalar(
        select(IPAssignment)
        .where(IPAssignment.id == command.assignment_id)
        .with_for_update()
    )
    if assignment is None:
        _error("assignment_not_found", "The reviewed IP assignment no longer exists.")
    # Same lock order as repair_service_ipv4_assignment (subscriber, address,
    # inventory) so a concurrent link or allocation waits instead of racing.
    if assignment.subscriber_id is not None:
        db.scalar(
            select(Subscriber)
            .where(Subscriber.id == assignment.subscriber_id)
            .with_for_update()
        )
    if assignment.ipv4_address_id is not None:
        db.scalar(
            select(IPv4Address)
            .where(IPv4Address.id == assignment.ipv4_address_id)
            .with_for_update()
        )
    _lock_eligibility_inventory(db)
    prior = _prior_unbound_release_outcome(
        db, command=command, idempotency_key=idempotency_key
    )
    if prior is not None:
        return prior

    current = preview_unbound_ip_assignment_release(
        db,
        assignment_id=command.assignment_id,
        eligibility_class=command.eligibility_class,
    )
    if not secrets.compare_digest(current.fingerprint, command.preview_fingerprint):
        _error(
            "stale_preview",
            "IPAM evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if not current.applicable:
        _error(
            "unsafe_cohort",
            "The reviewed assignment is no longer safe to release.",
            decision=current.decision.value,
        )

    # The only write: retire the assignment. The address row, the pool, every
    # served column and RADIUS are untouched.
    assignment.is_active = False
    db.flush()
    stage_audit_event(
        db,
        action=_UNBOUND_RELEASE_ITEM_AUDIT_ACTION,
        entity_type="ip_assignment",
        entity_id=str(assignment.id),
        actor=AuditActor.service(command.context.actor),
        metadata={
            "eligibility_class": current.eligibility_class.value,
            "subscriber_id": (
                str(current.subscriber_id) if current.subscriber_id else None
            ),
            "ipv4_address_id": (
                str(current.address_id) if current.address_id else None
            ),
            "preview_fingerprint": current.fingerprint,
            "reason": command.context.reason,
        },
    )
    stage_audit_event(
        db,
        action=_UNBOUND_RELEASE_BATCH_AUDIT_ACTION,
        entity_type="ip_assignment_unbound_release",
        entity_id=idempotency_key,
        actor=AuditActor.service(command.context.actor),
        metadata={
            "assignment_id": str(assignment.id),
            "eligibility_class": current.eligibility_class.value,
            "preview_fingerprint": current.fingerprint,
            "released_count": 1,
            "reason": command.context.reason,
        },
    )
    emit_event(
        db,
        EventType.ip_assignment_unbound_released,
        {
            "schema_version": 1,
            "assignment_id": str(assignment.id),
            "eligibility_class": current.eligibility_class.value,
            "preview_fingerprint": current.fingerprint,
        },
        actor=command.context.actor,
    )
    db.flush()
    return UnboundIPAssignmentReleaseOutcome(
        assignment_id=assignment.id,
        eligibility_class=current.eligibility_class,
        released_count=1,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


def release_unbound_ip_assignment(
    db: Session,
    command: ReleaseUnboundIPAssignmentCommand,
) -> UnboundIPAssignmentReleaseOutcome:
    """Release one unbound IPv4 assignment under a named N9 exception class."""

    return execute_owner_command(
        db,
        definition=_UNBOUND_RELEASE_COMMAND,
        context=command.context,
        operation=lambda: _release_unbound_ip_assignment(db, command),
    )


__all__ = [
    "IPv4LiveUseEvidence",
    "find_ipv4_live_use",
    "ReleaseSubscriberIPAssignmentsCommand",
    "SubscriberIPAssignmentReleaseOutcome",
    "stage_subscriber_ip_assignment_release",
    "ActiveIPv4AssignmentEvidence",
    "IPAssignmentOwnershipDecision",
    "IPAssignmentOwnershipError",
    "IPAssignmentOwnershipItem",
    "IPAssignmentOwnershipOutcome",
    "IPAssignmentOwnershipPreview",
    "IPv4AssignmentLifecycleError",
    "IPv4AssignmentRepairDecision",
    "IPv4ServedProjectionDecision",
    "ReconcileIPAssignmentOwnershipCommand",
    "RepairServiceIPv4AssignmentCommand",
    "RepairServiceIPv4ProjectionCommand",
    "ServiceIPv4AssignmentRepairOutcome",
    "ServiceIPv4AssignmentRepairPreview",
    "ServiceIPv4ProjectionOutcome",
    "ServiceIPv4ProjectionPreview",
    "apply_service_ipv4_assignment_participant",
    "apply_service_ipv4_projection_participant",
    "preview_ip_assignment_service_ownership",
    "preview_service_ipv4_assignment_repair",
    "preview_service_ipv4_projection_repair",
    "reconcile_ip_assignment_service_ownership",
    "repair_service_ipv4_assignment",
    "repair_service_ipv4_projection",
    "ActiveIPv6AssignmentEvidence",
    "IPAssignmentServiceUnlinkDecision",
    "IPAssignmentServiceUnlinkOutcome",
    "IPv6AssignmentLinkDecision",
    "LinkServiceIPv6AssignmentCommand",
    "ServiceIPAssignmentUnlinkPreview",
    "ServiceIPv6AssignmentLinkOutcome",
    "ServiceIPv6AssignmentLinkPreview",
    "UnlinkIPAssignmentServiceOwnershipCommand",
    "link_service_ipv6_assignment",
    "preview_ip_assignment_service_unlink",
    "preview_service_ipv6_assignment_link",
    "unlink_ip_assignment_service_ownership",
    "ReleaseUnboundIPAssignmentCommand",
    "UnboundIPAssignmentReleaseOutcome",
    "UnboundIPv4ReleaseClass",
    "UnboundIPv4ReleaseDecision",
    "UnboundIPv4ReleasePreview",
    "UnboundIPv4ServiceEvidence",
    "preview_unbound_ip_assignment_release",
    "release_unbound_ip_assignment",
]
