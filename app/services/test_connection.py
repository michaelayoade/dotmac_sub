"""access.test_connection: audited, billing-independent troubleshooting grants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.system_user import SystemUser
from app.models.test_connection import TestConnectionGrant
from app.schemas.test_connection import TestConnectionCreated, TestConnectionReference
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.form_contracts import (
    FormConsequence,
    FormContract,
    FormPrerequisite,
    register,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.runtime_durable_timers import ScheduleTimerCommand, schedule_timer
from app.services.test_connection_policy import TestConnectionAccess, utc_datetime

OWNER = "access.test_connection"
PERMISSION = "subscription:test_connection"
EXPIRY_TRIGGER = "access.test_connection.expiry_due"
CONCERN = "bounded subscription test-access grants"
_ACTIVATE = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="activate_test_connection"
)
_EXPIRE = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="expire_test_connection"
)
_DELIVERY = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="record_test_connection_delivery"
)

TEST_CONNECTION_FORM = register(
    FormContract(
        key="admin.subscription_test_connection",
        title="Test Connection",
        entity="subscription",
        command_owner=OWNER,
        consequences=(
            FormConsequence(
                key="access",
                label="Full subscription access for the configured system duration.",
            ),
            FormConsequence(
                key="expiry",
                label="Normal subscription and billing restrictions apply again at expiry.",
            ),
        ),
    )
)


class TestConnectionDeliveryState(str, Enum):
    pending = "pending"
    applied = "applied"
    failed = "failed"


class TestConnectionError(DomainError):
    pass


def _error(suffix: str, message: str) -> TestConnectionError:
    return TestConnectionError(code=f"{OWNER}.{suffix}", message=message)


@dataclass(frozen=True, slots=True)
class TestConnectionQuery:
    subscription_ids: tuple[UUID, ...]
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class TestConnectionConfiguration:
    default_hours: int
    maximum_hours: int
    deadline_verified: bool


def configuration(db: Session) -> TestConnectionConfiguration:
    from app.services.settings_spec import resolve_integer, resolve_value

    maximum = resolve_integer(db, SettingDomain.radius, "test_connection_maximum_hours")
    default = resolve_integer(db, SettingDomain.radius, "test_connection_default_hours")
    verified = resolve_value(
        db, SettingDomain.radius, "test_connection_deadline_verified"
    )
    return TestConnectionConfiguration(
        default_hours=min(default, maximum),
        maximum_hours=maximum,
        deadline_verified=verified is True or str(verified).lower() == "true",
    )


def current_access(
    db: Session, *, query: TestConnectionQuery
) -> tuple[TestConnectionAccess, ...]:
    if not query.subscription_ids:
        return ()
    now = utc_datetime(query.evaluated_at)
    rows = db.scalars(
        select(TestConnectionGrant).where(
            TestConnectionGrant.subscription_id.in_(query.subscription_ids),
            TestConnectionGrant.ended_at.is_(None),
            TestConnectionGrant.activated_at <= now,
            TestConnectionGrant.expires_at > now,
        )
    ).all()
    terminal_ids = set(
        db.scalars(
            select(Subscription.id).where(
                Subscription.id.in_(query.subscription_ids),
                Subscription.status.in_(
                    (
                        SubscriptionStatus.canceled,
                        SubscriptionStatus.hidden,
                        SubscriptionStatus.archived,
                    )
                ),
            )
        ).all()
    )
    # An explicit fraud lock overrides troubleshooting access. Financial locks
    # remain recorded but cannot shorten or deny a valid grant.
    fraud_ids = set(
        db.scalars(
            select(EnforcementLock.subscription_id).where(
                EnforcementLock.subscription_id.in_(query.subscription_ids),
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason == EnforcementReason.fraud,
            )
        ).all()
    )
    return tuple(
        TestConnectionAccess(
            grant_id=row.id,
            subscription_id=row.subscription_id,
            activated_at=utc_datetime(row.activated_at),
            expires_at=utc_datetime(row.expires_at),
        )
        for row in rows
        if row.subscription_id not in fraud_ids | terminal_ids
    )


def access_for_subscription(
    db: Session, subscription_id: UUID
) -> TestConnectionAccess | None:
    accesses = current_access(
        db,
        query=TestConnectionQuery(
            subscription_ids=(subscription_id,), evaluated_at=datetime.now(UTC)
        ),
    )
    return accesses[0] if accesses else None


@dataclass(frozen=True, slots=True)
class TestConnectionPreviewQuery:
    subscriber_id: UUID
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class TestConnectionPreview:
    subscription_id: UUID
    subscription_label: str
    duration_hours: int
    maximum_hours: int
    expected_expiry: datetime
    prerequisites: tuple[FormPrerequisite, ...]
    current: TestConnectionOutcome | None

    @property
    def can_activate(self) -> bool:
        return all(item.met for item in self.prerequisites)


def preview_test_connection(
    db: Session, *, query: TestConnectionPreviewQuery
) -> TestConnectionPreview:
    subscription = db.scalar(
        select(Subscription).where(
            Subscription.id == query.subscription_id,
            Subscription.subscriber_id == query.subscriber_id,
        )
    )
    if subscription is None:
        raise _error(
            "subscription_not_found", "Subscription does not belong to this customer."
        )
    config = configuration(db)
    hours = config.default_hours
    if not 1 <= hours <= config.maximum_hours:
        raise _error(
            "invalid_duration",
            f"Choose a duration between 1 and {config.maximum_hours} hours.",
        )
    now = datetime.now(UTC)
    running = db.scalar(
        select(TestConnectionGrant).where(
            TestConnectionGrant.subscription_id == subscription.id,
            TestConnectionGrant.ended_at.is_(None),
            TestConnectionGrant.expires_at > now,
        )
    )
    prerequisites = [
        FormPrerequisite(
            key="deadline",
            label="Automatic network expiry verified",
            met=config.deadline_verified,
            reason=None
            if config.deadline_verified
            else "Network operations must verify the RADIUS/NAS deadline configuration before testing.",
        ),
        FormPrerequisite(
            key="running",
            label="No test already running",
            met=running is None,
            reason="A test is already running; activation cannot extend its expiry."
            if running
            else None,
        ),
    ]
    try:
        _validate_network_identity(db, subscription)
        if subscription.status in {
            SubscriptionStatus.canceled,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        }:
            raise _error(
                "network_not_ready",
                "The service is canceled or has not been provisioned.",
            )
        fraud = db.scalar(
            select(EnforcementLock.id).where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason == EnforcementReason.fraud,
            )
        )
        if fraud:
            raise _error(
                "security_hold", "An explicit fraud/security hold requires review."
            )
        prerequisites.append(
            FormPrerequisite(
                key="network", label="Service network identity ready", met=True
            )
        )
    except TestConnectionError as exc:
        prerequisites.append(
            FormPrerequisite(
                key="network",
                label="Service network identity ready",
                met=False,
                reason=exc.message,
            )
        )
    return TestConnectionPreview(
        subscription_id=subscription.id,
        subscription_label=str(subscription.login or subscription.id),
        duration_hours=hours,
        maximum_hours=config.maximum_hours,
        expected_expiry=now + timedelta(hours=hours),
        prerequisites=tuple(prerequisites),
        current=_outcome(running) if running else None,
    )


def current_summaries(
    db: Session, *, query: TestConnectionQuery
) -> tuple[TestConnectionOutcome, ...]:
    if not query.subscription_ids:
        return ()
    grants = db.scalars(
        select(TestConnectionGrant).where(
            TestConnectionGrant.subscription_id.in_(query.subscription_ids),
            TestConnectionGrant.ended_at.is_(None),
            TestConnectionGrant.expires_at > query.evaluated_at,
        )
    ).all()
    return tuple(_outcome(grant) for grant in grants)


@dataclass(frozen=True, slots=True)
class ActivateTestConnectionCommand:
    context: CommandContext
    subscriber_id: UUID
    subscription_id: UUID
    actor_id: UUID


@dataclass(frozen=True, slots=True)
class TestConnectionOutcome:
    grant_id: UUID
    subscription_id: UUID
    activated_at: datetime
    expires_at: datetime
    duration_seconds: int
    delivery_state: TestConnectionDeliveryState
    replayed: bool = False


def _outcome(
    grant: TestConnectionGrant, *, replayed: bool = False
) -> TestConnectionOutcome:
    return TestConnectionOutcome(
        grant_id=grant.id,
        subscription_id=grant.subscription_id,
        activated_at=utc_datetime(grant.activated_at),
        expires_at=utc_datetime(grant.expires_at),
        duration_seconds=grant.duration_seconds,
        delivery_state=TestConnectionDeliveryState(grant.delivery_state),
        replayed=replayed,
    )


def _validate_network_identity(db: Session, subscription: Subscription) -> None:
    from app.services.credential_crypto import decrypt_credential
    from app.services.external_radius_targets import active_external_radius_targets
    from app.services.radius_access_state import (
        TERMINATED_STATUSES,
        UNPROVISIONED_STATUSES,
    )

    login = str(subscription.login or "").strip()
    if not login:
        raise _error(
            "network_not_ready",
            "This service needs a provisioned RADIUS login before connectivity testing.",
        )
    credential = db.scalar(
        select(AccessCredential).where(
            AccessCredential.username == login,
            AccessCredential.subscriber_id == subscription.subscriber_id,
        )
    )
    if (
        credential is None
        or credential.subscription_id not in (None, subscription.id)
        or not credential.secret_hash
    ):
        raise _error(
            "network_not_ready",
            "This service has no usable, uniquely owned network credential.",
        )
    if credential.subscription_id is None:
        siblings = db.scalars(
            select(Subscription.id).where(
                Subscription.login == login,
                Subscription.id != subscription.id,
                ~Subscription.status.in_(TERMINATED_STATUSES | UNPROVISIONED_STATUSES),
            )
        ).all()
        if siblings:
            raise _error(
                "ambiguous_login",
                "This login is shared by multiple live subscriptions. Select the subscription with its own network credential before testing.",
            )
    try:
        password = decrypt_credential(credential.secret_hash)
    except Exception as exc:
        raise _error(
            "network_not_ready", "The service network credential cannot be recovered."
        ) from exc
    if not password or subscription.offer is None:
        raise _error(
            "network_not_ready",
            "The service needs a usable credential and service plan.",
        )
    targets = active_external_radius_targets(db, capability="users")
    if not targets:
        raise _error("network_not_ready", "No RADIUS projection target is configured.")
    from sqlalchemy.engine import make_url

    if any(
        make_url(str(target["db_url"])).get_backend_name() != "postgresql"
        for target in targets
    ):
        raise _error(
            "network_not_ready",
            "Test Connection requires the verified PostgreSQL RADIUS deadline queries on every user target.",
        )


def _stage_finance_creation_event(
    db: Session,
    *,
    grant: TestConnectionGrant,
    context: CommandContext,
) -> None:
    """Freeze one customer's rolling creation count in the grant transaction."""
    from app.services.events.dispatcher import emit_event as stage_creation_event

    end = utc_datetime(grant.activated_at)
    start = end - timedelta(days=7)
    filters = (
        TestConnectionGrant.subscriber_id == grant.subscriber_id,
        TestConnectionGrant.activated_at > start,
        TestConnectionGrant.activated_at <= end,
    )
    count = int(
        db.scalar(select(func.count()).select_from(TestConnectionGrant).where(*filters))
        or 0
    )
    previous = db.scalars(
        select(TestConnectionGrant)
        .where(*filters, TestConnectionGrant.id != grant.id)
        .order_by(
            TestConnectionGrant.activated_at.desc(), TestConnectionGrant.id.desc()
        )
        .limit(9)
    ).all()
    evidence = TestConnectionCreated(
        tenant_id=OPERATOR_TENANT_ID,
        grant_id=grant.id,
        subscription_id=grant.subscription_id,
        customer_id=grant.subscriber_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.causation_id,
        created_at=end,
        window_start=start,
        window_end=end,
        count_7d=count,
        recent_connections=tuple(
            TestConnectionReference(
                grant_id=item.id,
                created_at=utc_datetime(item.activated_at),
                created_by=item.actor_label,
                duration_seconds=item.duration_seconds,
            )
            for item in (grant, *previous)
        ),
    )
    stage_creation_event(
        db,
        EventType.test_connection_created,
        evidence.model_dump(mode="json"),
        event_id=uuid5(
            NAMESPACE_URL, f"subscription-test-connection-created:{grant.id}"
        ),
        actor=context.actor,
        account_id=grant.subscriber_id,
        subscription_id=grant.subscription_id,
        dispatch_after_commit=False,
    )


def activate_test_connection(
    db: Session, *, command: ActivateTestConnectionCommand
) -> TestConnectionOutcome:
    def operation() -> TestConnectionOutcome:
        if command.context.scope != PERMISSION or command.context.actor != str(
            command.actor_id
        ):
            raise _error(
                "permission_denied",
                "Test Connection requires an authorized staff principal.",
            )
        actor = db.get(SystemUser, command.actor_id)
        if actor is None or not actor.is_active:
            raise _error(
                "permission_denied", "The staff principal is inactive or missing."
            )
        subscription = db.scalar(
            select(Subscription)
            .where(
                Subscription.id == command.subscription_id,
                Subscription.subscriber_id == command.subscriber_id,
            )
            .with_for_update()
        )
        if subscription is None:
            raise _error(
                "subscription_not_found",
                "Subscription does not belong to this customer.",
            )
        previous = db.scalar(
            select(TestConnectionGrant).where(
                TestConnectionGrant.command_id == command.context.command_id
            )
        )
        if previous is not None:
            if (
                previous.subscription_id != command.subscription_id
                or previous.actor_id != command.actor_id
            ):
                raise _error(
                    "idempotency_conflict",
                    "The request identity has already been used for another activation.",
                )
            return _outcome(previous, replayed=True)
        # Serialize all subscriptions belonging to this customer before choosing
        # the activation/creation time. Same-subscription replays remain under
        # the existing subscription lock and return before this point.
        if db.get_bind().dialect.name == "postgresql":
            lock_id = uuid5(
                NAMESPACE_URL, f"test-connection-customer:{subscription.subscriber_id}"
            )
            lock_key = int.from_bytes(lock_id.bytes[:8], "big", signed=True)
            db.execute(select(func.pg_advisory_xact_lock(lock_key)))
        config = configuration(db)
        if not config.deadline_verified:
            raise _error(
                "deadline_not_verified",
                "Network test-expiry enforcement must be verified in RADIUS settings before activation.",
            )
        duration_hours = config.default_hours
        if not 1 <= duration_hours <= config.maximum_hours:
            raise _error(
                "invalid_duration",
                f"Choose a duration between 1 and {config.maximum_hours} hours.",
            )
        now = datetime.now(UTC).replace(microsecond=0)
        open_grant = db.scalar(
            select(TestConnectionGrant)
            .where(
                TestConnectionGrant.subscription_id == subscription.id,
                TestConnectionGrant.ended_at.is_(None),
            )
            .with_for_update()
        )
        if open_grant is not None:
            if utc_datetime(open_grant.expires_at) > now:
                raise _error(
                    "already_active",
                    "Test Connection is already running for this subscription.",
                )
            _finish(db, open_grant, context=command.context, now=now)
            db.flush()
        fraud = db.scalar(
            select(EnforcementLock.id).where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason == EnforcementReason.fraud,
            )
        )
        if fraud:
            raise _error(
                "security_hold",
                "An explicit fraud/security hold must be reviewed before testing.",
            )
        # Terminal deletion/cancellation is not a billing suspension. Disabled,
        # expired, blocked, paused and long-overdue services remain testable.
        if subscription.status in {
            SubscriptionStatus.canceled,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        }:
            raise _error(
                "network_not_ready",
                "This service is canceled or has not been provisioned.",
            )
        _validate_network_identity(db, subscription)
        actor_label = (
            actor.display_name or f"{actor.first_name} {actor.last_name}".strip()
        )
        grant = TestConnectionGrant(
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            actor_id=actor.id,
            actor_label=actor_label,
            command_id=command.context.command_id,
            activated_at=now,
            expires_at=now + timedelta(hours=duration_hours),
            duration_seconds=duration_hours * 3600,
            delivery_state="pending",
        )
        db.add(grant)
        db.flush()
        schedule_timer(
            db,
            ScheduleTimerCommand(
                owner=OWNER,
                entity_kind="test_connection_grant",
                entity_id=grant.id,
                purpose="expiry",
                due_at=grant.expires_at,
                output_event_type=EXPIRY_TRIGGER,
            ),
            context=command.context,
        )
        stage_audit_event(
            db,
            action="subscription.test_connection_activated",
            entity_type="subscription",
            entity_id=str(subscription.id),
            actor=AuditActor.user(
                str(actor.id), label=actor_label, party_id=actor.person_party_id
            ),
            occurred_at=now,
            metadata={
                "grant_id": str(grant.id),
                "duration_seconds": grant.duration_seconds,
                "activated_at": now.isoformat(),
                "expires_at": grant.expires_at.isoformat(),
            },
            request_id=str(command.context.correlation_id),
        )
        emit_event(
            db,
            EventType.subscription_test_connection_changed,
            {
                "schema_version": 1,
                "grant_id": str(grant.id),
                "subscription_id": str(subscription.id),
                "transition": "activated",
            },
            actor=str(actor.id),
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        _stage_finance_creation_event(db, grant=grant, context=command.context)
        return _outcome(grant)

    return execute_owner_command(
        db, definition=_ACTIVATE, context=command.context, operation=operation
    )


def _finish(
    db: Session, grant: TestConnectionGrant, *, context: CommandContext, now: datetime
) -> None:
    grant.ended_at = now
    stage_audit_event(
        db,
        action="subscription.test_connection_expired",
        entity_type="subscription",
        entity_id=str(grant.subscription_id),
        actor=AuditActor.system(OWNER),
        metadata={
            "grant_id": str(grant.id),
            "duration_seconds": grant.duration_seconds,
            "activated_at": utc_datetime(grant.activated_at).isoformat(),
            "expires_at": utc_datetime(grant.expires_at).isoformat(),
        },
        occurred_at=now,
    )
    emit_event(
        db,
        EventType.subscription_test_connection_changed,
        {
            "schema_version": 1,
            "grant_id": str(grant.id),
            "subscription_id": str(grant.subscription_id),
            "transition": "expired",
        },
        actor=context.actor,
        subscription_id=grant.subscription_id,
        account_id=grant.subscriber_id,
    )


@dataclass(frozen=True, slots=True)
class ExpireTestConnectionCommand:
    context: CommandContext
    grant_id: UUID


def expire_test_connection(
    db: Session, *, command: ExpireTestConnectionCommand
) -> TestConnectionOutcome:
    def operation() -> TestConnectionOutcome:
        grant = db.scalar(
            select(TestConnectionGrant)
            .where(TestConnectionGrant.id == command.grant_id)
            .with_for_update()
        )
        if grant is None:
            raise _error("grant_not_found", "Test Connection grant was not found.")
        now = datetime.now(UTC)
        if grant.ended_at is not None:
            return _outcome(grant, replayed=True)
        if now < utc_datetime(grant.expires_at):
            raise _error("not_due", "Test Connection has not expired.")
        _finish(db, grant, context=command.context, now=now)
        return _outcome(grant)

    return execute_owner_command(
        db, definition=_EXPIRE, context=command.context, operation=operation
    )


@dataclass(frozen=True, slots=True)
class RecordTestConnectionDeliveryCommand:
    context: CommandContext
    grant_id: UUID
    applied: bool
    error_code: str | None = None


def record_delivery(
    db: Session, *, command: RecordTestConnectionDeliveryCommand
) -> TestConnectionOutcome:
    def operation() -> TestConnectionOutcome:
        grant = db.scalar(
            select(TestConnectionGrant)
            .where(TestConnectionGrant.id == command.grant_id)
            .with_for_update()
        )
        if grant is None:
            raise _error("grant_not_found", "Test Connection grant was not found.")
        if grant.ended_at is None and utc_datetime(grant.expires_at) > datetime.now(
            UTC
        ):
            previous_state = grant.delivery_state
            grant.delivery_state = "applied" if command.applied else "failed"
            grant.delivery_error = (
                None
                if command.applied
                else (command.error_code or "network_delivery_failed")[:160]
            )
            grant.applied_at = datetime.now(UTC) if command.applied else None
            if previous_state != grant.delivery_state:
                stage_audit_event(
                    db,
                    action="subscription.test_connection_delivery_"
                    + grant.delivery_state,
                    entity_type="subscription",
                    entity_id=str(grant.subscription_id),
                    actor=AuditActor.system(OWNER),
                    is_success=command.applied,
                    metadata={
                        "grant_id": str(grant.id),
                        "delivery_state": grant.delivery_state,
                        "error_code": grant.delivery_error,
                    },
                )
        return _outcome(grant)

    return execute_owner_command(
        db, definition=_DELIVERY, context=command.context, operation=operation
    )


@dataclass(frozen=True, slots=True)
class TestConnectionNetworkQuery:
    subscription_id: UUID
    grant_id: UUID


@dataclass(frozen=True, slots=True)
class TestConnectionNetworkOutcome:
    subscription_id: UUID
    disconnected_sessions: int
    active_grant_id: UUID | None


def reconcile_test_connection_network(
    db: Session, *, query: TestConnectionNetworkQuery
) -> TestConnectionNetworkOutcome:
    from app.services.enforcement import (
        apply_subscription_address_list_block,
        disconnect_subscription_sessions,
        remove_subscription_address_list_block,
    )
    from app.services.radius_population import (
        reconcile_usernames,
        require_complete_projection,
    )
    from app.services.radius_projection_planner import plan_login_radius_projections

    subscription = db.get(Subscription, query.subscription_id)
    if subscription is None or not subscription.login:
        raise _error(
            "network_not_ready", "The test subscription has no network identity."
        )
    current = access_for_subscription(db, subscription.id)
    grant = db.get(TestConnectionGrant, query.grant_id)
    if grant is None or grant.subscription_id != subscription.id:
        raise _error(
            "grant_not_found", "The network request does not match its test grant."
        )
    if current is not None and (
        current.grant_id != query.grant_id or grant.delivery_state == "applied"
    ):
        # Old events must not interrupt a newer test. A duplicate successful
        # activation needs no second disconnect; permanent drift repair still
        # compares/rebuilds the authoritative projection independently.
        return TestConnectionNetworkOutcome(subscription.id, 0, current.grant_id)
    result = reconcile_usernames({subscription.login}, dry_run=False, source_db=db)
    require_complete_projection(result)
    plan = plan_login_radius_projections(db, [subscription])[subscription.login].plan
    if plan.mode == "active":
        remove_subscription_address_list_block(
            db, str(subscription.id), require_confirmed=True
        )
    else:
        apply_subscription_address_list_block(db, str(subscription.id))
    disconnected = disconnect_subscription_sessions(
        db,
        str(subscription.id),
        reason="test_connection_refresh",
        refresh_test_access=True,
        require_terminal=True,
        authoritative_only=True,
    )
    access = access_for_subscription(db, subscription.id)
    return TestConnectionNetworkOutcome(
        subscription_id=subscription.id,
        disconnected_sessions=disconnected,
        active_grant_id=access.grant_id if access else None,
    )
