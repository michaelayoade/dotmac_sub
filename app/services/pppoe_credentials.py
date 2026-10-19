"""PPPoE credential auto-generation service.

Generates PPPoE usernames from the subscriber canonical id and random passwords
when a subscription is activated and no active AccessCredential exists.
This always runs on subscription activation — PPPoE credentials are
mandatory for all subscribers.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from app.models.catalog import (
    AccessCredential,
    ConnectionType,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import numbering, settings_spec
from app.services.access_credential_secret import (
    UNUSABLE_STORED_SECRET_MESSAGE,
    is_secret_reference_value,
    reject_reserved_service_password,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.customer_identifiers import pppoe_username_from_subscriber_number
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# DocumentSequence key for the fallback username (non-canonical subscribers).
SEQUENCE_KEY = "pppoe_username"
_OWNER = "access.pppoe_credentials"


class PppoeCredentialError(DomainError):
    """Stable failure at the PPPoE credential owner boundary."""


class PppoeCredentialDisposition(StrEnum):
    created = "created"
    reused = "reused"
    rebound_legacy = "rebound_legacy"
    rebound_replacement = "rebound_replacement"
    reactivated = "reactivated"


@dataclass(frozen=True, slots=True)
class EnsurePppoeCredentialCommand:
    """Typed request to ensure one subscriber/service PPPoE identity."""

    subscriber_id: UUID
    subscription_id: UUID | None = None
    radius_profile_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EnsurePppoeCredentialOutcome:
    """Secret-free result of ensuring a PPPoE credential."""

    credential_id: UUID
    username: str
    disposition: PppoeCredentialDisposition
    changed: bool
    #: Secret-free reason a replacement did not inherit although an ended
    #: service held a credential (``ambiguous_candidates`` | ``other_live_service``).
    inherit_declined_reason: str | None = None


def _error(code: str, message: str) -> PppoeCredentialError:
    return PppoeCredentialError(
        code=f"{_OWNER}.{code}",
        message=message,
        details={},
        retryable=False,
    )


def _stage_credential_event(
    db: Session,
    *,
    command: EnsurePppoeCredentialCommand,
    outcome: EnsurePppoeCredentialOutcome,
) -> EnsurePppoeCredentialOutcome:
    if not outcome.changed:
        return outcome
    emit_event(
        db,
        EventType.access_credential_ensured,
        {
            "schema_version": 1,
            "credential_id": str(outcome.credential_id),
            "subscriber_id": str(command.subscriber_id),
            "subscription_id": str(command.subscription_id)
            if command.subscription_id
            else None,
            "radius_profile_id": str(command.radius_profile_id)
            if command.radius_profile_id
            else None,
            "disposition": outcome.disposition.value,
        },
        account_id=command.subscriber_id,
        subscription_id=command.subscription_id,
    )
    return outcome


def _generate_random_password(length: int) -> str:
    """Generate a cryptographically random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _resolve_int_setting(value: object, fallback: int) -> int:
    """Coerce a setting value to int with a fallback."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return fallback
    return fallback


def _resolve_radius_setting(db: Session, key: str) -> object | None:
    """Resolve a radius setting directly from the database for activation-time consistency."""
    spec = settings_spec.get_spec(SettingDomain.radius, key)
    if not spec:
        return None

    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    raw = settings_spec.extract_db_value(setting)
    if raw is None:
        raw = spec.default

    value, error = settings_spec.coerce_value(spec, raw)
    if error:
        value = spec.default
    if spec.allowed and value is not None and value not in spec.allowed:
        value = spec.default
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = _resolve_int_setting(
            value, spec.default if isinstance(spec.default, int) else 1
        )
        if spec.min_value is not None and parsed < spec.min_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        if spec.max_value is not None and parsed > spec.max_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        value = parsed
    return value


_INHERITABLE_STATUSES = {
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
}
# Statuses where a service has genuinely ended; anything else is a live service.
_ENDED_STATUSES = _INHERITABLE_STATUSES | {
    SubscriptionStatus.archived,
    SubscriptionStatus.hidden,
}


def _is_live_binding(db: Session, credential: AccessCredential) -> bool:
    """True when the credential serves a service that has not ended."""
    if credential.subscription_id is None:
        return True
    bound = db.get(Subscription, credential.subscription_id)
    return bound is None or bound.status not in _ENDED_STATUSES


def _login_moved_to_live_service(
    db: Session,
    command: EnsurePppoeCredentialCommand,
    subscription: Subscription,
) -> bool:
    """True when ``subscription.login`` is an ACTIVE credential of the same
    customer that is bound to another, non-ended service."""
    holders = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == command.subscriber_id,
            AccessCredential.username == subscription.login,
            AccessCredential.is_active.is_(True),
            AccessCredential.subscription_id.is_not(None),
            AccessCredential.subscription_id != command.subscription_id,
        )
        .all()
    )
    return any(_is_live_binding(db, holder) for holder in holders)


def legacy_unbound_candidate(
    db: Session, subscriber_id: UUID, subscription_id: UUID
) -> AccessCredential | None:
    """The customer's single unbound legacy credential, or None.

    Explicit rule (never a guess): exactly ONE credential of the customer has
    ``subscription_id IS NULL`` and this service is the customer's ONLY live
    service. Staff must still choose to adopt it.
    """
    unbound = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == subscriber_id,
            AccessCredential.subscription_id.is_(None),
        )
        .all()
    )
    if len(unbound) != 1:
        return None
    others = (
        db.query(Subscription)
        .filter(
            Subscription.subscriber_id == subscriber_id,
            Subscription.id != subscription_id,
        )
        .all()
    )
    if any(other.status not in _ENDED_STATUSES for other in others):
        return None
    return unbound[0]


def _inherit_enabled(db: Session) -> bool:
    return _resolve_radius_setting(db, "pppoe_credential_on_new_service") == "inherit"


def _inherit_statuses(db: Session) -> set[SubscriptionStatus]:
    """Configured ended statuses a replacement may inherit from (fail closed)."""
    raw = _resolve_radius_setting(db, "pppoe_credential_inherit_from_statuses")
    if not isinstance(raw, str):
        raw = "disabled,canceled,expired"
    names = {item.strip().lower() for item in raw.split(",") if item.strip()}
    statuses = {status for status in _INHERITABLE_STATUSES if status.value in names}
    return statuses


def _has_usable_secret(credential: AccessCredential) -> bool:
    if not credential.secret_hash:
        return False
    if is_secret_reference_value(credential.secret_hash):
        return False
    try:
        return bool(decrypt_credential(credential.secret_hash))
    except Exception:
        return False


def _default_password_length(db: Session) -> int:
    raw = _resolve_radius_setting(db, "pppoe_default_password_length")
    return max(8, min(64, _resolve_int_setting(raw, 12)))


def _generate_pppoe_username(db: Session, subscriber_id: UUID) -> str | None:
    from app.models.subscriber import Subscriber

    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is not None:
        derived = pppoe_username_from_subscriber_number(
            db, subscriber.subscriber_number
        )
        if derived:
            return derived
    # Fall back to a sequential username when the subscriber number isn't in the
    # canonical SUB-<digits> shape (imported/manual records). PPPoE credentials
    # are mandatory for activation, so generation must always yield a username —
    # otherwise non-canonical subscribers could never be activated.
    return _generate_pppoe_username_sequence(db)


def generate_pppoe_username(db: Session, subscriber_id: UUID) -> str | None:
    """Username activation would derive for ``subscriber_id`` (public alias)."""
    return _generate_pppoe_username(db, subscriber_id)


def username_in_use(
    db: Session, username: str, *, exclude_id: UUID | None = None
) -> bool:
    query = db.query(AccessCredential).filter(AccessCredential.username == username)
    if exclude_id is not None:
        query = query.filter(AccessCredential.id != exclude_id)
    return query.first() is not None


def plan_change_provisioning(db: Session, subscription_id: UUID) -> bool:
    """True while a plan change is provisioning on this subscription."""
    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
    )

    return (
        db.query(SubscriptionChangeRequest.id)
        .filter(
            SubscriptionChangeRequest.subscription_id == subscription_id,
            SubscriptionChangeRequest.execution_state
            == SubscriptionChangeExecutionState.provisioning,
        )
        .first()
        is not None
    )


def stage_pending_credential(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    username: str,
    plain_password: str | None,
    radius_profile_id: UUID | None,
) -> None:
    """Stage typed login/password on an inactive credential bound to this service.

    Flush-only; the caller owns the commit. Activation activates the staged row
    as typed instead of generating new credentials. Only this subscription's own staged row is touched; there is
    no subscriber-level fallback.
    """
    reject_reserved_service_password(plain_password)
    staged = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == subscriber_id,
            AccessCredential.subscription_id == subscription_id,
            AccessCredential.is_active.is_(False),
        )
        .order_by(AccessCredential.created_at.desc())
        .first()
    )
    if username_in_use(db, username, exclude_id=staged.id if staged else None):
        raise ValueError(
            f"Service login {username} is already used by another customer or "
            "credential."
        )
    secret_hash = encrypt_credential(plain_password) if plain_password else None
    if staged is None:
        db.add(
            AccessCredential(
                subscriber_id=subscriber_id,
                subscription_id=subscription_id,
                username=username,
                secret_hash=secret_hash,
                is_active=False,
                radius_profile_id=radius_profile_id,
            )
        )
    else:
        staged.username = username
        if secret_hash:
            staged.secret_hash = secret_hash
        if radius_profile_id:
            staged.radius_profile_id = radius_profile_id
    db.flush()


def _generate_pppoe_username_sequence(db: Session) -> str | None:
    prefix_value = _resolve_radius_setting(db, "pppoe_username_prefix")
    padding_value = _resolve_radius_setting(db, "pppoe_username_padding")
    start_value = _resolve_radius_setting(db, "pppoe_username_start")
    return numbering.generate_number_with_config(
        db,
        SEQUENCE_KEY,
        prefix=prefix_value if isinstance(prefix_value, str) else None,
        padding=_resolve_int_setting(padding_value, 5),
        start_value=_resolve_int_setting(start_value, 1),
    )


def ensure_pppoe_credential(
    db: Session,
    command: EnsurePppoeCredentialCommand,
) -> EnsurePppoeCredentialOutcome:
    """Ensure one active PPPoE credential and its exact service binding.

    This participant is idempotent and flush-only. The lifecycle, catalog, or
    provisioning coordinator that admitted ``command`` owns transaction
    completion. Credential secrets never leave this boundary.
    """
    from app.models.subscriber import Subscriber

    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == command.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        raise _error("subscriber_missing", "The target subscriber was not found.")

    subscription = None
    if command.subscription_id is not None:
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise _error(
                "subscription_missing", "The target subscription was not found."
            )
        if subscription.subscriber_id != command.subscriber_id:
            raise _error(
                "subscriber_mismatch",
                "The target subscription no longer belongs to the subscriber.",
            )

    inherit_declined_reason: str | None = None
    if command.radius_profile_id is not None:
        profile = db.get(RadiusProfile, command.radius_profile_id)
        if profile is None or not profile.is_active:
            raise _error(
                "radius_profile_unavailable",
                "The target RADIUS profile is unavailable.",
            )

    existing_query = db.query(AccessCredential).filter(
        AccessCredential.subscriber_id == command.subscriber_id,
        AccessCredential.is_active.is_(True),
    )
    if command.subscription_id is not None:
        existing = (
            existing_query.filter(
                AccessCredential.subscription_id == command.subscription_id
            )
            .with_for_update()
            .first()
        )
        if existing is None:
            staged = (
                db.query(AccessCredential)
                .filter(
                    AccessCredential.subscriber_id == command.subscriber_id,
                    AccessCredential.subscription_id == command.subscription_id,
                    AccessCredential.is_active.is_(False),
                )
                .order_by(AccessCredential.created_at.desc())
                .with_for_update()
                .first()
            )
            if staged is not None:
                if is_secret_reference_value(staged.secret_hash):
                    raise _error(
                        "secret_reference_refused", UNUSABLE_STORED_SECRET_MESSAGE
                    )
                # Login/password typed on the create form were staged on an
                # inactive credential bound to this exact service; activate it
                # as typed. The secret is never read or logged here.
                if not staged.secret_hash:
                    staged.secret_hash = encrypt_credential(
                        _generate_random_password(_default_password_length(db))
                    )
                staged.is_active = True
                staged.connection_type = ConnectionType.pppoe
                staged.radius_profile_id = command.radius_profile_id
                if subscription is not None:
                    subscription.login = staged.username
                db.flush()
                return _stage_credential_event(
                    db,
                    command=command,
                    outcome=EnsurePppoeCredentialOutcome(
                        credential_id=staged.id,
                        username=staged.username,
                        disposition=PppoeCredentialDisposition.reactivated,
                        changed=True,
                    ),
                )
            if (
                subscription is not None
                and str(subscription.login or "").strip()
                and _inherit_enabled(db)
                and _login_moved_to_live_service(db, command, subscription)
            ):
                # Known limitation (pending the Starter disabled->SUSPENDED
                # mapping): this service's credential was rebound to a LIVE
                # replacement. Restoring it must neither issue a second
                # credential nor take the first back. A pre-set login that
                # matches an ENDED predecessor (bulk activate, API create) is
                # the normal replacement and falls through to the inherit rule.
                raise _error(
                    "credential_rebound_elsewhere",
                    "This service's PPPoE credential now belongs to a "
                    "replacement service. Set the credential setting to 'new' "
                    "or issue this service its own login manually.",
                )
            legacy = (
                existing_query.filter(AccessCredential.subscription_id.is_(None))
                .with_for_update()
                .all()
            )
            if len(legacy) == 1:
                existing = legacy[0]
                existing.subscription_id = command.subscription_id
                existing.radius_profile_id = command.radius_profile_id
                existing.connection_type = ConnectionType.pppoe
                if subscription is not None:
                    subscription.login = existing.username
                db.flush()
                return _stage_credential_event(
                    db,
                    command=command,
                    outcome=EnsurePppoeCredentialOutcome(
                        credential_id=existing.id,
                        username=existing.username,
                        disposition=PppoeCredentialDisposition.rebound_legacy,
                        changed=True,
                    ),
                )

            replacement = (
                existing_query.join(
                    Subscription,
                    AccessCredential.subscription_id == Subscription.id,
                )
                .filter(Subscription.status == SubscriptionStatus.disabled)
                .with_for_update()
                .all()
            )
            active_credentials = existing_query.with_for_update().all()
            if len(active_credentials) == 1 and len(replacement) == 1:
                existing = replacement[0]
                existing.subscription_id = command.subscription_id
                existing.radius_profile_id = command.radius_profile_id
                existing.connection_type = ConnectionType.pppoe
                if subscription is not None:
                    subscription.login = existing.username
                db.flush()
                return _stage_credential_event(
                    db,
                    command=command,
                    outcome=EnsurePppoeCredentialOutcome(
                        credential_id=existing.id,
                        username=existing.username,
                        disposition=PppoeCredentialDisposition.rebound_replacement,
                        changed=True,
                    ),
                )

            if _inherit_enabled(db):
                candidates = (
                    db.query(AccessCredential)
                    .join(
                        Subscription,
                        AccessCredential.subscription_id == Subscription.id,
                    )
                    .filter(
                        AccessCredential.subscriber_id == command.subscriber_id,
                        Subscription.subscriber_id == command.subscriber_id,
                        Subscription.id != command.subscription_id,
                        Subscription.status.in_(_inherit_statuses(db)),
                    )
                    .with_for_update()
                    .all()
                )
                if candidates:
                    live = [
                        c
                        for c in existing_query.with_for_update().all()
                        if _is_live_binding(db, c)
                    ]
                    if len(candidates) == 1 and not live:
                        inherited = candidates[0]
                        if is_secret_reference_value(inherited.secret_hash):
                            raise _error(
                                "secret_reference_refused",
                                UNUSABLE_STORED_SECRET_MESSAGE,
                            )
                        inherited.subscription_id = command.subscription_id
                        inherited.radius_profile_id = command.radius_profile_id
                        inherited.connection_type = ConnectionType.pppoe
                        inherited.is_active = True
                        if subscription is not None:
                            subscription.login = inherited.username
                        db.flush()
                        return _stage_credential_event(
                            db,
                            command=command,
                            outcome=EnsurePppoeCredentialOutcome(
                                credential_id=inherited.id,
                                username=inherited.username,
                                disposition=(
                                    PppoeCredentialDisposition.rebound_replacement
                                ),
                                changed=True,
                            ),
                        )
                    inherit_declined_reason = (
                        "other_live_service" if live else "ambiguous_candidates"
                    )
                    logger.info(
                        "PPPoE credential inheritance declined",
                        extra={
                            "subscription_id": str(command.subscription_id),
                            "reason": inherit_declined_reason,
                        },
                    )
    else:
        existing = existing_query.with_for_update().first()
    if existing:
        changed = existing.connection_type != ConnectionType.pppoe
        changed = changed or (
            command.radius_profile_id is not None
            and existing.radius_profile_id != command.radius_profile_id
        )
        changed = changed or (
            subscription is not None and subscription.login != existing.username
        )
        existing.connection_type = ConnectionType.pppoe
        if command.radius_profile_id is not None:
            existing.radius_profile_id = command.radius_profile_id
        if subscription is not None:
            subscription.login = existing.username
        db.flush()
        return _stage_credential_event(
            db,
            command=command,
            outcome=EnsurePppoeCredentialOutcome(
                credential_id=existing.id,
                username=existing.username,
                disposition=PppoeCredentialDisposition.reused,
                changed=changed,
            ),
        )

    has_other_service_credential = (
        command.subscription_id is not None
        and existing_query.filter(
            AccessCredential.subscription_id.is_not(None),
            AccessCredential.subscription_id != command.subscription_id,
        ).first()
        is not None
    )
    username = (
        _generate_pppoe_username_sequence(db)
        if has_other_service_credential
        else _generate_pppoe_username(db, command.subscriber_id)
    )
    if not username:
        raise _error(
            "username_unavailable",
            "A PPPoE username could not be allocated for the subscription.",
        )

    plain_password = _generate_random_password(_default_password_length(db))
    encrypted_password = encrypt_credential(plain_password)

    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.username == username)
        .with_for_update()
        .first()
    )
    if credential is not None and credential.subscriber_id != command.subscriber_id:
        raise _error(
            "username_conflict",
            "The derived PPPoE username is already assigned to another subscriber.",
        )

    disposition = PppoeCredentialDisposition.reactivated
    if credential is None:
        credential = AccessCredential(
            subscriber_id=command.subscriber_id,
            subscription_id=command.subscription_id,
            username=username,
            is_active=True,
            connection_type=ConnectionType.pppoe,
        )
        db.add(credential)
        disposition = PppoeCredentialDisposition.created

    if disposition is PppoeCredentialDisposition.reactivated and (
        _inherit_enabled(db) and _has_usable_secret(credential)
    ):
        # Reactivation keeps the customer's existing password so the CPE
        # keeps authenticating; only the "new" mode overwrites (legacy).
        pass
    else:
        credential.secret_hash = encrypted_password
    credential.subscription_id = command.subscription_id
    credential.is_active = True
    credential.connection_type = ConnectionType.pppoe
    credential.radius_profile_id = command.radius_profile_id
    if subscription is not None:
        subscription.login = username

    db.flush()
    logger.info(
        "Ensured PPPoE credential",
        extra={
            "credential_id": str(credential.id),
            "subscription_id": str(command.subscription_id)
            if command.subscription_id
            else None,
            "disposition": disposition.value,
        },
    )
    return _stage_credential_event(
        db,
        command=command,
        outcome=EnsurePppoeCredentialOutcome(
            credential_id=credential.id,
            username=credential.username,
            disposition=disposition,
            changed=True,
            inherit_declined_reason=inherit_declined_reason,
        ),
    )


__all__ = [
    "EnsurePppoeCredentialCommand",
    "EnsurePppoeCredentialOutcome",
    "PppoeCredentialDisposition",
    "PppoeCredentialError",
    "ensure_pppoe_credential",
    "generate_pppoe_username",
    "legacy_unbound_candidate",
    "plan_change_provisioning",
    "stage_pending_credential",
    "username_in_use",
]
