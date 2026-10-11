"""Credential-standing owner for password login, MFA completion and change.

Why this module exists
----------------------
Password success, MFA completion and session issuance used to be separate
transactions that never re-checked the credential. A reset that committed
between the password check and the session INSERT left a session the reset
never revoked, and an MFA challenge carried no binding to the credential it
was minted for.

Design (see the race-fix design, section numbers in comments):

* Phase A is read-only and runs in the adapter (``auth_flow``): resolve the
  credential, check the lock, verify the password, decide eligibility. It ends
  with an immutable snapshot (``VerifiedCredential``/``Challenge``). No lock is
  held while hashing.
* Phase B is ONE ``execute_owner_command`` here. Its first write is a
  conditional ``UPDATE ... WHERE id AND credential_version AND ... RETURNING``
  that is the commit gate: if a reset holds the row, the UPDATE waits and
  PostgreSQL re-evaluates the predicate against the new row version, returning
  zero rows. The session INSERT and audit rows are staged in the same
  transaction, so a session can never be committed without the credential
  standing that justified it.
* ``credential_version`` names the secret and its standing; ``password_hash``
  is only a representation of it (``replace_password_representation`` is the
  one writer that changes it without a bump).

Nothing here is transport-aware; refusals are
``PasswordAuthenticationError`` with a stable ``kind`` that adapters map.
PPPoE (``access_credential``) authentication never touches ``user_credentials``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from jose import JWTError
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.catalog import AccessCredential
from app.models.radius import RadiusUser
from app.services import staff_party_authentication
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.orm import InstrumentedAttribute

    from app.services.auth_flow import StagedSession

logger = logging.getLogger(__name__)

OWNER = "auth.password_authentication"
SCOPE = "authentication:password"

CONCERN_PASSWORD_STEP = "password authentication success transition"
CONCERN_MFA = "MFA challenge completion"
CONCERN_FAILURES = "authentication failure counters"
CONCERN_CHANGE = "authenticated password change"

_PASSWORD_STEP_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_PASSWORD_STEP, name="complete_password_step"
)
_MFA_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_MFA, name="complete_mfa_challenge"
)
_ENROLLMENT_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_MFA, name="confirm_mfa_enrollment"
)
_DENIAL_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_FAILURES, name="record_authentication_denial"
)
_CHANGE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_CHANGE, name="change_password"
)

_IMPERSONATION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN_PASSWORD_STEP,
    name="establish_customer_impersonation_session",
)
_REVOKE_DELIVERY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN_PASSWORD_STEP,
    name="revoke_undelivered_customer_session",
)

SRC_USER_CREDENTIAL = "user_credential"
SRC_ACCESS_CREDENTIAL = "access_credential"
SRC_RADIUS_USER = "radius_user"
_SOURCES = frozenset({SRC_USER_CREDENTIAL, SRC_ACCESS_CREDENTIAL, SRC_RADIUS_USER})
_PRINCIPAL_TYPES = frozenset({"subscriber", "system_user", "reseller_user"})
_AUDIENCES = frozenset({"general", "admin"})
CHALLENGE_VERSION = 2
CHALLENGE_TTL = timedelta(minutes=5)
LOCK_TIMEOUT = "5s"
LOCK_TIMEOUT_RETRY_AFTER_SECONDS = 5

#: Postgres SQLSTATEs the owner maps deliberately.
_LOCK_NOT_AVAILABLE = "55P03"
_DEADLOCK_DETECTED = "40P01"


class PasswordAuthenticationError(DomainError):
    """Stable, transport-neutral refusal. ``kind`` is what adapters map.

    Kinds: ``invalid_credentials``, ``locked``, ``must_change_password``,
    ``account_disabled``, ``admin_required``, ``invalid_mfa_token``,
    ``invalid_mfa_code``, ``mfa_locked``, ``credential_changed``,
    ``lock_timeout``.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code=f"{OWNER}.{kind}",
            message=message,
            details=details,
            retryable=kind == "lock_timeout",
        )
        self.kind = kind


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedCredential:
    """Phase A result: what was verified, and which version it was verified at."""

    src: str
    principal_type: str
    principal_id: str
    audience: str
    staff_binding: staff_party_authentication.StaffSessionBinding | None
    mfa_required: bool
    enrollment_required: bool
    credential_id: UUID | None = None
    credential_version: int | None = None
    provider: AuthProvider | None = None
    #: Representation seen at verification. Not part of any gate predicate (a
    #: representation-only rehash must not invalidate a parallel login); it is
    #: the compare value for the later upgrade hook and the PPPoE secret check.
    verified_hash: str | None = None
    access_credential_id: UUID | None = None
    #: ``updated_at`` of the PPPoE credential at verification; becomes the
    #: ``acu`` claim of an MFA challenge so a changed secret voids it.
    access_credential_updated_at: datetime | None = None
    radius_user_id: UUID | None = None
    radius_user_updated_at: datetime | None = None


@dataclass(frozen=True)
class Challenge:
    """A decoded, structurally valid v2 MFA / enrollment challenge."""

    typ: str
    principal_id: str
    principal_type: str
    party_id: str | None
    audience: str
    src: str
    credential_id: UUID | None
    credential_version: int | None
    access_credential_id: UUID | None
    access_credential_updated_us: int | None
    radius_user_id: UUID | None = None
    radius_user_updated_us: int | None = None


@dataclass(frozen=True)
class FailurePolicy:
    max_attempts: int
    lock_minutes: int


@dataclass(frozen=True, slots=True)
class PortalAuthenticationSourceQuery:
    identifier: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PortalRadiusSnapshot:
    id: UUID
    updated_at: datetime | None
    access_credential_id: UUID | None


@dataclass(frozen=True, slots=True)
class PortalAccessSnapshot:
    id: UUID
    updated_at: datetime | None
    secret_hash: str | None = field(repr=False)
    subscriber_id: UUID | None


@dataclass(frozen=True, slots=True)
class PortalAuthenticationSources:
    radius: PortalRadiusSnapshot | None
    access: PortalAccessSnapshot | None


def resolve_portal_authentication_sources(
    db: Session,
    query: PortalAuthenticationSourceQuery,
) -> PortalAuthenticationSources:
    """Read-only canonical source snapshots before external verification.

    A linked access record belongs to the RADIUS source; username fallback is
    resolved only when no active RADIUS source exists, preserving source policy.
    No session or ORM row crosses this query boundary.
    """
    radius = db.scalar(
        select(RadiusUser)
        .where(
            RadiusUser.username == query.identifier,
            RadiusUser.is_active.is_(True),
        )
        .limit(1)
    )
    if radius is not None:
        access = (
            db.get(AccessCredential, radius.access_credential_id)
            if radius.access_credential_id
            else None
        )
    else:
        access = db.scalar(
            select(AccessCredential)
            .where(
                AccessCredential.username == query.identifier,
                AccessCredential.is_active.is_(True),
            )
            .limit(1)
        )
    return PortalAuthenticationSources(
        radius=PortalRadiusSnapshot(
            radius.id, radius.updated_at, radius.access_credential_id
        )
        if radius is not None
        else None,
        access=PortalAccessSnapshot(
            access.id, access.updated_at, access.secret_hash, access.subscriber_id
        )
        if access is not None
        else None,
    )


class DenialReason(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    LOCKED = "locked"
    RESET_REQUIRED = "must_change_password"
    DISABLED = "account_disabled"
    AUDIENCE = "admin_required"
    PROJECTION = "projection_unavailable"
    INVALID_CHALLENGE = "invalid_mfa_token"
    INVALID_CODE = "invalid_mfa_code"
    MFA_LOCKED = "mfa_locked"
    METHOD = "mfa_method_unavailable"
    CREDENTIAL_CHANGED = "credential_changed"
    OWNER_UNAVAILABLE = "owner_unavailable"
    LOCK_TIMEOUT = "lock_timeout"
    SESSION_ADMISSION = "session_admission_denied"
    ENROLLMENT_REQUIRED = "enrollment_required"


class DenialSurface(StrEnum):
    LOGIN = "login"
    CUSTOMER_PORTAL = "customer_portal"
    VENDOR_API = "vendor_api"
    VENDOR_WEB = "vendor_web"
    RESELLER = "reseller"
    ENROLLMENT = "enrollment"
    PASSWORD_CHANGE = "password_change"
    PROFILE_STEP_UP = "profile_step_up"
    INSPECTOR_STEP_UP = "inspector_step_up"


class DenialPhase(StrEnum):
    PASSWORD = "password"
    MFA = "mfa"
    ENROLLMENT = "enrollment"
    OWNER = "owner"
    SESSION_ADMISSION = "session_admission"
    STEP_UP = "step_up"


class DenialTargetKind(StrEnum):
    CREDENTIAL = "user_credential"
    ACCESS = "access_credential"
    RADIUS = "radius_user"
    METHOD = "mfa_method"
    PRINCIPAL = "principal"


class DenialProvenance(StrEnum):
    RESOLVED_RECORD = "resolved_record"
    VERIFIED_CHALLENGE = "verified_challenge"
    AUTHENTICATED_PRINCIPAL = "authenticated_principal"


@dataclass(frozen=True, slots=True)
class DenialTarget:
    """Resolved identity evidence; this does not assert successful authentication."""

    kind: DenialTargetKind
    identifier: UUID
    provenance: DenialProvenance = DenialProvenance.RESOLVED_RECORD

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DenialTargetKind) or not isinstance(
            self.provenance, DenialProvenance
        ):
            raise TypeError("A denial target requires bounded kind and provenance")
        if not isinstance(self.identifier, UUID):
            raise TypeError("A denial target requires a resolved UUID")


@dataclass(frozen=True, slots=True)
class PasswordFailureEffect:
    credential_id: UUID
    policy: FailurePolicy


@dataclass(frozen=True, slots=True)
class MfaFailureEffect:
    method_id: UUID
    policy: FailurePolicy


@dataclass(frozen=True, slots=True)
class AuthenticationDenial:
    reason: DenialReason
    surface: DenialSurface
    phase: DenialPhase
    target: DenialTarget | None
    attempt_id: UUID
    correlation_id: UUID
    trusted_ip: IPv4Address | IPv6Address | None = None
    effect: PasswordFailureEffect | MfaFailureEffect | None = None
    status_code: Literal[400, 401, 403, 404, 409, 428, 429, 503] = 401

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reason, DenialReason)
            or not isinstance(self.surface, DenialSurface)
            or not isinstance(self.phase, DenialPhase)
        ):
            raise TypeError("Authentication denial classification must be bounded")
        if not isinstance(self.attempt_id, UUID) or not isinstance(
            self.correlation_id, UUID
        ):
            raise TypeError("Authentication denial context requires UUIDs")
        if self.status_code not in {400, 401, 403, 404, 409, 428, 429, 503}:
            raise ValueError("Authentication denial status is not bounded")
        if self.trusted_ip is not None and not isinstance(
            self.trusted_ip, (IPv4Address, IPv6Address)
        ):
            raise TypeError("Authentication denial IP must be normalized")
        if self.effect is not None:
            expected = (
                DenialTargetKind.CREDENTIAL
                if isinstance(self.effect, PasswordFailureEffect)
                else DenialTargetKind.METHOD
            )
            identifier = (
                self.effect.credential_id
                if isinstance(self.effect, PasswordFailureEffect)
                else self.effect.method_id
            )
            if (
                self.target is None
                or self.target.kind is not expected
                or self.target.identifier != identifier
            ):
                raise ValueError("Failure effect must match the resolved denial target")


def denial_for_request(
    *,
    reason: DenialReason,
    surface: DenialSurface,
    phase: DenialPhase,
    target: DenialTarget | None,
    request: Request | None = None,
    effect: PasswordFailureEffect | MfaFailureEffect | None = None,
    status_code: Literal[400, 401, 403, 404, 409, 428, 429, 503] = 401,
) -> AuthenticationDenial:
    """Normalize request evidence once; never retain request or raw headers."""
    correlation = uuid.uuid4()
    trusted_ip = None
    if request is not None:
        try:
            correlation = UUID(request.headers.get("x-request-id", ""))
        except (ValueError, TypeError, AttributeError):
            pass
        try:
            trusted_ip = ip_address(request.client.host) if request.client else None
        except (ValueError, TypeError, AttributeError):
            pass
    return AuthenticationDenial(
        reason=reason,
        surface=surface,
        phase=phase,
        target=target,
        attempt_id=uuid.uuid4(),
        correlation_id=correlation,
        trusted_ip=trusted_ip,
        effect=effect,
        status_code=status_code,
    )


def challenge_denial_target(db: Session, challenge: Challenge) -> DenialTarget | None:
    """Resolve a signature-validated envelope to an existing canonical record.

    Signed UUID claims alone are not proof that a target exists. Snapshot this
    result before entering an owner command, whose rollback may expire ORM rows.
    """
    model: type[UserCredential] | type[RadiusUser] | type[AccessCredential]
    if challenge.src == SRC_USER_CREDENTIAL:
        kind, model, identifier = (
            DenialTargetKind.CREDENTIAL,
            UserCredential,
            challenge.credential_id,
        )
    elif challenge.src == SRC_RADIUS_USER:
        kind, model, identifier = (
            DenialTargetKind.RADIUS,
            RadiusUser,
            challenge.radius_user_id,
        )
    else:
        kind, model, identifier = (
            DenialTargetKind.ACCESS,
            AccessCredential,
            challenge.access_credential_id,
        )
    if identifier is None:
        return None
    resolved = db.scalar(select(model.id).where(model.id == identifier))
    return (
        DenialTarget(kind, resolved, DenialProvenance.VERIFIED_CHALLENGE)
        if resolved is not None
        else None
    )


@dataclass(frozen=True)
class PasswordStepOutcome:
    """``kind`` is ``session`` | ``mfa_challenge`` | ``enrollment_challenge``."""

    kind: str
    credential_version: int | None
    staged_session: StagedSession | None = None
    challenge_token: str | None = None


@dataclass(frozen=True)
class EnrollmentOutcome:
    """Committed forced enrollment: the staged session and the one-time codes."""

    staged_session: Any
    recovery_codes: tuple[str, ...]


@dataclass(frozen=True)
class PasswordChangeOutcome:
    changed_at: datetime
    credential_version: int
    principal_type: str
    principal_id: str
    revoked_session_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_flow():
    # Lazy: auth_flow imports this module at import time.
    from app.services import auth_flow

    return auth_flow


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def release_read_transaction(db: Session) -> None:
    """End phase A: assert it was read-only, then clear the read transaction.

    Phase A is read-only by contract. A caller that arrives with pending ORM
    writes is a programming error: rolling them back silently would discard
    them, committing them would smuggle them into the credential transaction.
    """

    pending = (
        list(db.new)
        + list(db.deleted)
        + [obj for obj in db.dirty if db.is_modified(obj)]
    )
    if pending:
        raise RuntimeError(
            "password authentication phase A must be read-only; the session "
            f"has {len(pending)} pending change(s)"
        )
    if db.in_transaction():
        db.rollback()


def _is_postgresql(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _set_lock_timeout(db: Session) -> None:
    """Bound every row-lock wait in this transaction (first phase B statement)."""

    if _is_postgresql(db):
        db.execute(text(f"SELECT set_config('lock_timeout', '{LOCK_TIMEOUT}', true)"))


def _sqlstate(exc: OperationalError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _context(principal_type: str, principal_id: str, reason: str) -> CommandContext:
    return CommandContext.system(
        actor=f"principal:{principal_type}:{principal_id}",
        scope=SCOPE,
        reason=reason,
    )


def _run(
    db: Session,
    definition: OwnerCommandDefinition,
    context: CommandContext,
    operation,
):
    """Run one owner command; retry a deadlock once, map a lock timeout.

    The gate re-evaluates ``credential_version`` on retry, so phase A's
    verification stays valid without re-hashing.
    """

    for attempt in range(2):
        try:
            return execute_owner_command(
                db, definition=definition, context=context, operation=operation
            )
        except OperationalError as exc:
            state = _sqlstate(exc)
            if state == _LOCK_NOT_AVAILABLE:
                raise PasswordAuthenticationError(
                    "lock_timeout",
                    "The credential is busy; retry shortly.",
                    details={"retry_after": LOCK_TIMEOUT_RETRY_AFTER_SECONDS},
                ) from exc
            if state == _DEADLOCK_DETECTED and attempt == 0:
                logger.warning(
                    "password_authentication_deadlock_retry",
                    extra={"event": "password_authentication_deadlock_retry"},
                )
                continue
            raise
    raise RuntimeError("unreachable owner command retry state")


def _epoch_us(value: datetime) -> int:
    delta = _as_utc(value) - datetime(1970, 1, 1, tzinfo=UTC)  # type: ignore[operator]
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_epoch_us(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)


def access_credential_marker(value: datetime | None) -> int | None:
    """The ``acu`` challenge claim for an access credential's ``updated_at``."""

    return None if value is None else _epoch_us(value)


# ---------------------------------------------------------------------------
# Challenge encode / decode (section 6)
# ---------------------------------------------------------------------------


def _challenge_payload(
    verified: VerifiedCredential,
    *,
    typ: str,
    credential_version: int | None,
    now: datetime,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "typ": typ,
        "ver": CHALLENGE_VERSION,
        "sub": verified.principal_id,
        "principal_id": verified.principal_id,
        "principal_type": verified.principal_type,
        # Not "aud": python-jose validates a registered `aud` claim against an
        # expected audience and rejects every decode that does not pass one,
        # which would break the other `typ` decoders of this token.
        "login_aud": verified.audience,
        "src": verified.src,
        "iat": int(now.timestamp()),
        "exp": int((now + CHALLENGE_TTL).timestamp()),
        # Log correlation only; the challenge is not single-use (follow-up).
        "jti": str(uuid.uuid4()),
    }
    if verified.principal_type == "system_user":
        if verified.staff_binding is None:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_missing,
                verified.principal_id,
            )
        if str(verified.staff_binding.system_user_id) != str(verified.principal_id):
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                verified.principal_id,
            )
        payload["party_id"] = str(verified.staff_binding.party_id)
    if verified.src == SRC_USER_CREDENTIAL:
        payload["cid"] = str(verified.credential_id)
        payload["cv"] = int(credential_version or 0)
    else:
        payload["acid"] = str(verified.access_credential_id)
        payload["acu"] = access_credential_marker(verified.access_credential_updated_at)
    if verified.src == SRC_RADIUS_USER:
        payload["rid"] = str(verified.radius_user_id)
        payload["ru"] = access_credential_marker(verified.radius_user_updated_at)
    return payload


def _invalid_challenge() -> PasswordAuthenticationError:
    return PasswordAuthenticationError("invalid_mfa_token", "Invalid MFA token.")


def _int_claim(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_challenge()
    return value


def _uuid_claim(payload: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(payload.get(key)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _invalid_challenge() from exc


def decode_challenge(db: Session | None, token: str, *, expected_typ: str) -> Challenge:
    """Decode and structurally validate a v2 challenge. No legacy fallback.

    A token without ``ver == 2`` and the credential binding for its ``src`` is
    refused: logins in flight at deployment (at most five minutes) restart.
    """

    af = _auth_flow()
    try:
        payload = af._jwt_decode_token(  # noqa: SLF001
            token,
            af._jwt_secret(db),
            af._jwt_algorithm(db),  # noqa: SLF001
        )
    except JWTError as exc:
        raise _invalid_challenge() from exc
    if payload.get("typ") != expected_typ:
        raise _invalid_challenge()
    if _int_claim(payload, "ver") != CHALLENGE_VERSION:
        raise _invalid_challenge()
    principal_id = payload.get("principal_id") or payload.get("sub")
    if (
        not principal_id
        or not isinstance(principal_id, str)
        or payload.get("sub") not in (None, principal_id)
    ):
        raise _invalid_challenge()
    principal_type = payload.get("principal_type")
    if principal_type not in _PRINCIPAL_TYPES:
        raise _invalid_challenge()
    audience = payload.get("login_aud")
    if audience not in _AUDIENCES:
        raise _invalid_challenge()
    src = payload.get("src")
    if src not in _SOURCES:
        raise _invalid_challenge()
    credential_id = credential_version = access_id = access_us = None
    if src == SRC_USER_CREDENTIAL:
        credential_id = _uuid_claim(payload, "cid")
        credential_version = _int_claim(payload, "cv")
        if credential_version < 1:
            raise _invalid_challenge()
    else:
        if principal_type != "subscriber":
            raise _invalid_challenge()
        access_id = _uuid_claim(payload, "acid")
        access_us = _int_claim(payload, "acu")
    radius_id = radius_us = None
    if src == SRC_RADIUS_USER:
        radius_id = _uuid_claim(payload, "rid")
        radius_us = _int_claim(payload, "ru")
    return Challenge(
        typ=expected_typ,
        principal_id=principal_id,
        principal_type=principal_type,
        party_id=str(payload["party_id"]) if payload.get("party_id") else None,
        audience=audience,
        src=src,
        credential_id=credential_id,
        credential_version=credential_version,
        access_credential_id=access_id,
        access_credential_updated_us=access_us,
        radius_user_id=radius_id,
        radius_user_updated_us=radius_us,
    )


# ---------------------------------------------------------------------------
# Principal re-check inside phase B (no lock; login never locks a principal)
# ---------------------------------------------------------------------------


def _lock_principal_key_share(
    db: Session, *, principal_type: str, principal_id: str
) -> None:
    """Take ``FOR KEY SHARE`` on the principal row BEFORE the credential gate.

    Staging a session INSERTs ``sessions`` with a foreign key to the principal,
    which makes PostgreSQL take exactly this lock implicitly. Reset locks the
    principal ``FOR UPDATE`` and then the credential row. Without taking this
    lock first, login would hold the credential row (gate) and then wait for
    the principal row while reset holds the principal row and waits for the
    credential row: a deadlock cycle in which reset is the likely victim.
    Taking it first gives login the same principal -> credential order as
    reset. It is shared with other logins and conflicts only with reset-style
    ``FOR UPDATE``. (The design said login never locks a principal row; the
    implicit foreign-key lock was missed. Architect to confirm this fix versus
    reset taking ``FOR NO KEY UPDATE``.)
    """

    if not _is_postgresql(db):
        return
    from app.models.subscriber import ResellerUser, Subscriber
    from app.models.system_user import SystemUser
    from app.services.common import coerce_uuid

    principal_pk: InstrumentedAttribute[uuid.UUID]
    if principal_type == "system_user":
        principal_pk = SystemUser.id
    elif principal_type == "reseller_user":
        principal_pk = ResellerUser.id
    else:
        principal_pk = Subscriber.id
    db.execute(
        select(principal_pk)
        .where(principal_pk == coerce_uuid(principal_id))
        .with_for_update(read=True, key_share=True)
    ).first()


def _reload_principal(
    db: Session,
    *,
    principal_type: str,
    principal_id: str,
    staff_binding: staff_party_authentication.StaffSessionBinding | None,
    refusal_kind: str,
) -> object:
    """Re-read the principal in the gate's transaction; refuse when unresolvable."""

    from app.models.subscriber import ResellerUser, Subscriber
    from app.services.common import coerce_uuid

    if principal_type == "system_user":
        if staff_binding is None:
            raise PasswordAuthenticationError(refusal_kind, "Account unavailable.")
        try:
            return staff_party_authentication.resolve_staff_principal_by_party(
                db,
                staff_binding.party_id,
                staff_binding.system_user_id,
                reference=staff_binding.system_user_id,
            )
        except staff_party_authentication.StaffProjectionError as exc:
            logger.error(
                "Staff authentication refused: %s (subject=%s)",
                exc.refusal.value,
                exc.credential_id,
            )
            raise PasswordAuthenticationError(
                refusal_kind, "Account unavailable."
            ) from exc
    model = ResellerUser if principal_type == "reseller_user" else Subscriber
    principal = db.get(model, coerce_uuid(principal_id))
    if principal is None:
        raise PasswordAuthenticationError(refusal_kind, "Account unavailable.")
    return principal


def _check_eligibility(
    db: Session,
    *,
    principal_type: str,
    principal_id: str,
    staff_binding: staff_party_authentication.StaffSessionBinding | None,
    audience: str,
    staff_refusal_kind: str,
) -> object:
    af = _auth_flow()
    principal = _reload_principal(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
        staff_binding=staff_binding,
        refusal_kind=staff_refusal_kind,
    )
    refusal = af.principal_refusal(
        principal_type, principal, af.LoginAudience(audience)
    )
    if refusal is not None:
        raise PasswordAuthenticationError(refusal, "Account unavailable.")
    return principal


def _audit_actor(
    principal_type: str,
    principal_id: str,
    staff_binding: staff_party_authentication.StaffSessionBinding | None,
) -> AuditActor:
    return AuditActor.user(
        principal_id,
        party_id=staff_binding.party_id if staff_binding is not None else None,
    )


def _stage_audit(
    db: Session,
    *,
    action: str,
    principal_type: str,
    principal_id: str,
    staff_binding: staff_party_authentication.StaffSessionBinding | None,
    metadata: dict[str, Any],
) -> None:
    """Stage an audit row. Metadata carries identifiers only: no secret, hash,
    token, code or raw request body."""

    stage_audit_event(
        db,
        action=action,
        entity_type=principal_type,
        entity_id=principal_id,
        actor=_audit_actor(principal_type, principal_id, staff_binding),
        metadata={"schema_version": 1, **metadata},
    )


# ---------------------------------------------------------------------------
# 4.1 Password step
# ---------------------------------------------------------------------------


def _classify_gate_miss(
    db: Session, credential_id: UUID, now: datetime
) -> PasswordAuthenticationError:
    """Zero rows from the gate: re-read WITHOUT a lock and say why.

    Never increments a counter and never re-verifies: the verification was
    already paid for, and re-hashing here would be an amplification path.
    """

    row = db.execute(
        select(
            UserCredential.locked_until,
            UserCredential.must_change_password,
        ).where(UserCredential.id == credential_id)
    ).first()
    if row is not None:
        locked_until = _as_utc(row.locked_until)
        if locked_until is not None and locked_until > now:
            return PasswordAuthenticationError(
                "locked", "Account locked.", details={"locked_until": locked_until}
            )
        if row.must_change_password:
            return PasswordAuthenticationError(
                "must_change_password", "Password reset required."
            )
    return PasswordAuthenticationError("invalid_credentials", "Invalid credentials.")


def _gate_password_step(
    db: Session, verified: VerifiedCredential, now: datetime
) -> int | None:
    if verified.src == SRC_RADIUS_USER:
        _gate_radius_standing(
            db,
            principal_id=verified.principal_id,
            access_id=verified.access_credential_id,
            access_us=access_credential_marker(verified.access_credential_updated_at),
            radius_id=verified.radius_user_id,
            radius_us=access_credential_marker(verified.radius_user_updated_at),
        )
        return None
    if verified.src == SRC_ACCESS_CREDENTIAL:
        # R8: PPPoE secret authentication never touches user_credentials. Only
        # confirm the secret that was verified is still the live one.
        access_row = db.execute(
            select(AccessCredential.id)
            .where(
                AccessCredential.id == verified.access_credential_id,
                AccessCredential.subscriber_id == UUID(verified.principal_id),
                AccessCredential.is_active.is_(True),
                AccessCredential.secret_hash == verified.verified_hash,
                AccessCredential.updated_at == verified.access_credential_updated_at,
            )
            # FOR SHARE, after the principal KEY SHARE: a concurrent secret
            # change (ORM UPDATE, FOR NO KEY UPDATE) can no longer commit
            # between this check and the commit of the session. After a wait
            # PostgreSQL re-evaluates the predicate on the new row version.
            .with_for_update(read=True)
        ).first()
        if access_row is None:
            raise PasswordAuthenticationError(
                "invalid_credentials", "Invalid credentials."
            )
        return None

    values: dict[str, Any] = {"failed_login_attempts": 0, "locked_until": None}
    if not (verified.mfa_required or verified.enrollment_required):
        values["last_login_at"] = now
    row = db.execute(
        update(UserCredential)
        .where(
            UserCredential.id == verified.credential_id,
            UserCredential.credential_version == verified.credential_version,
            UserCredential.is_active.is_(True),
            UserCredential.provider == verified.provider,
            UserCredential.must_change_password.is_not(True),
            or_(
                UserCredential.locked_until.is_(None),
                UserCredential.locked_until <= now,
            ),
        )
        .values(**values)
        .returning(UserCredential.credential_version)
        .execution_options(synchronize_session=False)
    ).first()
    if row is None:
        raise _classify_gate_miss(db, verified.credential_id, now)  # type: ignore[arg-type]
    return int(row[0])


def complete_password_step(
    db: Session,
    verified: VerifiedCredential,
    *,
    request: Request,
    session_sink: Literal["auth", "customer_portal"] = "auth",
    expires_at: datetime | None = None,
) -> PasswordStepOutcome:
    """Phase B of the password step: gate, then session OR challenge, + audit."""

    af = _auth_flow()
    if session_sink == "customer_portal" and (
        verified.principal_type != "subscriber" or expires_at is None
    ):
        raise ValueError("Customer portal sessions require subscriber and expiry")
    release_read_transaction(db)

    def operation() -> PasswordStepOutcome:
        _set_lock_timeout(db)
        now = _now()
        if verified.src != SRC_USER_CREDENTIAL or not (
            verified.mfa_required or verified.enrollment_required
        ):
            _lock_principal_key_share(
                db,
                principal_type=verified.principal_type,
                principal_id=verified.principal_id,
            )
        credential_version = _gate_password_step(db, verified, now)
        _check_eligibility(
            db,
            principal_type=verified.principal_type,
            principal_id=verified.principal_id,
            staff_binding=verified.staff_binding,
            audience=verified.audience,
            staff_refusal_kind="account_disabled",
        )
        # B-PR4 hook point: replace_password_representation(...) would be
        # called HERE, after the gate and in this transaction. Deliberately
        # not called by login in this change.
        evidence: dict[str, Any] = {
            "credential_id": str(verified.credential_id)
            if verified.credential_id
            else None,
            "credential_version": credential_version,
            "src": verified.src,
            "audience": verified.audience,
        }
        if verified.access_credential_id is not None:
            evidence["access_credential_id"] = str(verified.access_credential_id)
        if verified.mfa_required or verified.enrollment_required:
            typ = "mfa" if verified.mfa_required else "mfa_enrollment"
            token = af._jwt_encode_token(  # noqa: SLF001
                _challenge_payload(
                    verified,
                    typ=typ,
                    credential_version=credential_version,
                    now=now,
                ),
                af._jwt_secret(db),  # noqa: SLF001
                af._jwt_algorithm(db),  # noqa: SLF001
            )
            _stage_audit(
                db,
                action="auth.password_step_succeeded",
                principal_type=verified.principal_type,
                principal_id=verified.principal_id,
                staff_binding=verified.staff_binding,
                metadata={**evidence, "mfa": verified.mfa_required},
            )
            return PasswordStepOutcome(
                kind="mfa_challenge"
                if verified.mfa_required
                else "enrollment_challenge",
                credential_version=credential_version,
                challenge_token=token,
            )
        staged = af.stage_session_issue(
            db,
            principal_type=verified.principal_type,
            principal_id=verified.principal_id,
            request=request,
            staff_binding=verified.staff_binding,
            expires_at=expires_at,
        )
        _stage_audit(
            db,
            action="auth.login_succeeded",
            principal_type=verified.principal_type,
            principal_id=verified.principal_id,
            staff_binding=verified.staff_binding,
            metadata={**evidence, "session_id": staged.session_id, "mfa": False},
        )
        return PasswordStepOutcome(
            kind="session", credential_version=credential_version, staged_session=staged
        )

    return _run(
        db,
        _PASSWORD_STEP_COMMAND,
        _context(
            verified.principal_type,
            verified.principal_id,
            "Commit a verified password step",
        ),
        operation,
    )


# ---------------------------------------------------------------------------
# 4.2 / 4.3 MFA completion and forced enrollment
# ---------------------------------------------------------------------------


def _gate_radius_standing(
    db: Session,
    *,
    principal_id: str,
    access_id: UUID | None,
    access_us: int | None,
    radius_id: UUID | None,
    radius_us: int | None,
) -> None:
    # Access owner syncs its RadiusUser projection after changing AccessCredential.
    # Match that access -> radius order, under the principal key-share lock.
    access = db.execute(
        select(AccessCredential.id)
        .where(
            AccessCredential.id == access_id,
            AccessCredential.subscriber_id == UUID(principal_id),
            AccessCredential.is_active.is_(True),
            AccessCredential.updated_at == _from_epoch_us(access_us or 0),
        )
        .with_for_update()
    ).first()
    radius = (
        db.execute(
            select(RadiusUser.id)
            .where(
                RadiusUser.id == radius_id,
                RadiusUser.subscriber_id == UUID(principal_id),
                RadiusUser.access_credential_id == access_id,
                RadiusUser.is_active.is_(True),
                RadiusUser.updated_at == _from_epoch_us(radius_us or 0),
            )
            .with_for_update()
        ).first()
        if access
        else None
    )
    if radius is None:
        raise _invalid_challenge()


def _gate_challenge_standing(db: Session, challenge: Challenge, now: datetime) -> None:
    """The credential gate for a challenge. Nothing is consumed on a miss."""

    if challenge.src == SRC_RADIUS_USER:
        _gate_radius_standing(
            db,
            principal_id=challenge.principal_id,
            access_id=challenge.access_credential_id,
            access_us=challenge.access_credential_updated_us,
            radius_id=challenge.radius_user_id,
            radius_us=challenge.radius_user_updated_us,
        )
        return
    if challenge.src == SRC_USER_CREDENTIAL:
        # No password-lock predicate on purpose: third-party wrong guesses must
        # not abort a user who already proved the password.
        row = db.execute(
            update(UserCredential)
            .where(
                UserCredential.id == challenge.credential_id,
                UserCredential.credential_version == challenge.credential_version,
                UserCredential.is_active.is_(True),
                UserCredential.must_change_password.is_not(True),
            )
            .values(last_login_at=now)
            .returning(UserCredential.id)
            .execution_options(synchronize_session=False)
        ).first()
    else:
        row = db.execute(
            select(AccessCredential.id)
            .where(
                AccessCredential.id == challenge.access_credential_id,
                AccessCredential.subscriber_id == UUID(challenge.principal_id),
                AccessCredential.is_active.is_(True),
                AccessCredential.updated_at
                == _from_epoch_us(challenge.access_credential_updated_us or 0),
            )
            .with_for_update(read=True)
        ).first()
    if row is None:
        raise _invalid_challenge()


def _challenge_binding(
    challenge: Challenge,
) -> staff_party_authentication.StaffSessionBinding | None:
    if challenge.principal_type != "system_user":
        return None
    try:
        return staff_party_authentication.StaffSessionBinding(
            party_id=UUID(str(challenge.party_id)),
            system_user_id=UUID(str(challenge.principal_id)),
        )
    except (TypeError, ValueError) as exc:
        raise _invalid_challenge() from exc


def complete_mfa(
    db: Session,
    challenge: Challenge,
    *,
    request: Request,
    method_id: UUID,
    totp_ok: bool,
    recovery_code_hash: str | None,
    failure_policy: FailurePolicy,
    session_sink: Literal["auth", "customer_portal"] = "auth",
    expires_at: datetime | None = None,
) -> StagedSession:
    """Phase B of MFA completion; returns the ``StagedSession`` (committed).

    Lock order: credential -> mfa_recovery_codes -> mfa_methods -> sessions.
    A wrong second factor rolls everything back and raises the typed refusal.
    The adapter dispatches exactly one independent failure command afterward.
    """

    af = _auth_flow()
    if session_sink == "customer_portal" and (
        challenge.principal_type != "subscriber" or expires_at is None
    ):
        raise ValueError("Customer portal sessions require subscriber and expiry")
    release_read_transaction(db)
    binding = _challenge_binding(challenge)

    def operation() -> StagedSession:
        _set_lock_timeout(db)
        now = _now()
        _lock_principal_key_share(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
        )
        _gate_challenge_standing(db, challenge, now)
        _check_eligibility(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            staff_binding=binding,
            audience=challenge.audience,
            staff_refusal_kind="invalid_mfa_token",
        )
        if not totp_ok:
            spent = None
            if recovery_code_hash is not None:
                spent = db.execute(
                    update(MFARecoveryCode)
                    .where(
                        MFARecoveryCode.mfa_method_id == method_id,
                        MFARecoveryCode.code_hash == recovery_code_hash,
                        MFARecoveryCode.is_active.is_(True),
                        MFARecoveryCode.used_at.is_(None),
                    )
                    .values(used_at=now, is_active=False)
                    .returning(MFARecoveryCode.id)
                    .execution_options(synchronize_session=False)
                ).first()
            if spent is None:
                raise PasswordAuthenticationError(
                    "invalid_mfa_code", "Invalid MFA code."
                )
        updated = db.execute(
            update(MFAMethod)
            .where(
                MFAMethod.id == method_id,
                MFAMethod.is_active.is_(True),
                MFAMethod.enabled.is_(True),
                or_(MFAMethod.locked_until.is_(None), MFAMethod.locked_until <= now),
            )
            .values(failed_attempts=0, locked_until=None, last_used_at=now)
            .returning(MFAMethod.id)
            .execution_options(synchronize_session=False)
        ).first()
        if updated is None:
            locked = db.execute(
                select(MFAMethod.locked_until).where(MFAMethod.id == method_id)
            ).scalar()
            locked = _as_utc(locked)
            if locked is not None and locked > now:
                raise PasswordAuthenticationError(
                    "mfa_locked",
                    "Too many incorrect codes.",
                    details={"locked_until": locked},
                )
            raise _invalid_challenge()
        staged = af.stage_session_issue(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            request=request,
            staff_binding=binding,
            expires_at=expires_at,
        )
        _stage_audit(
            db,
            action="auth.login_succeeded",
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            staff_binding=binding,
            metadata={
                "credential_id": str(challenge.credential_id)
                if challenge.credential_id
                else None,
                "credential_version": challenge.credential_version,
                "access_credential_id": str(challenge.access_credential_id)
                if challenge.access_credential_id
                else None,
                "session_id": staged.session_id,
                "src": challenge.src,
                "mfa": True,
                "audience": challenge.audience,
            },
        )
        return staged

    context = _context(
        challenge.principal_type, challenge.principal_id, "Complete an MFA challenge"
    )
    return _run(db, _MFA_COMMAND, context, operation)


def challenge_has_standing(db: Session, challenge: Challenge) -> bool:
    """Read-only, lock-free check that a challenge's credential still stands.

    For callers that must not write anything until the gated owner command
    (for example showing the enrollment setup page). It is advisory; the gate
    inside ``confirm_mfa_enrollment`` is the authority.
    """

    if challenge.src != SRC_USER_CREDENTIAL:
        return False
    return (
        db.execute(
            select(UserCredential.id).where(
                UserCredential.id == challenge.credential_id,
                UserCredential.credential_version == challenge.credential_version,
                UserCredential.is_active.is_(True),
                UserCredential.must_change_password.is_not(True),
            )
        ).first()
        is not None
    )


def confirm_mfa_enrollment(
    db: Session,
    challenge: Challenge,
    *,
    request: Request,
    method_id: UUID,
    expected_secret: str,
    totp_ok: bool,
    failure_policy: FailurePolicy,
) -> EnrollmentOutcome:
    """Forced enrollment in ONE owner transaction, behind the credential gate.

    Principal KEY SHARE, then the credential gate, then eligibility, and only
    then the factor: refuse if a confirmed primary exists, activate this pending
    TOTP method as primary, stage recovery codes, session and audit. A version mismatch (a
    reset since the password step) refuses before any of it is written, and a
    later failure rolls the whole transaction back: no factor, no codes, no
    session. A wrong code rolls back and is counted in its own transaction.
    """

    af = _auth_flow()
    release_read_transaction(db)
    binding = _challenge_binding(challenge)
    if (
        challenge.src != SRC_USER_CREDENTIAL
        or challenge.principal_type != "system_user"
        or binding is None
    ):
        raise _invalid_challenge()

    def operation() -> EnrollmentOutcome:
        _set_lock_timeout(db)
        now = _now()
        _lock_principal_key_share(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
        )
        _gate_challenge_standing(db, challenge, now)
        _check_eligibility(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            staff_binding=binding,
            audience=challenge.audience,
            staff_refusal_kind="invalid_mfa_token",
        )
        system_user_id = UUID(str(challenge.principal_id))
        # Never replace a confirmed factor through an enrollment token: the
        # credential gate above serializes concurrent enrollments for this user
        # (it holds the credential row), so this check cannot race another one.
        existing = db.execute(
            select(MFAMethod.id).where(
                MFAMethod.system_user_id == system_user_id,
                MFAMethod.method_type == MFAMethodType.totp,
                MFAMethod.is_active.is_(True),
                MFAMethod.enabled.is_(True),
                MFAMethod.is_primary.is_(True),
            )
        ).first()
        if existing is not None:
            raise _invalid_challenge()
        # Checked before the code so a replayed token cannot drive failure
        # counts; only then is a wrong code a counted failure.
        if not totp_ok:
            raise PasswordAuthenticationError("invalid_mfa_code", "Invalid MFA code.")
        # No live primary exists. An admin MFA reset leaves the old row
        # is_primary=True (disabled, inactive); demote it so the unique primary
        # index admits the new one. A live primary was refused above.
        db.execute(
            update(MFAMethod)
            .where(
                MFAMethod.system_user_id == system_user_id,
                MFAMethod.id != method_id,
                MFAMethod.is_primary.is_(True),
            )
            .values(is_primary=False)
            .execution_options(synchronize_session=False)
        )
        activated = db.execute(
            update(MFAMethod)
            .where(
                MFAMethod.id == method_id,
                MFAMethod.system_user_id == system_user_id,
                MFAMethod.subscriber_id.is_(None),
                MFAMethod.method_type == MFAMethodType.totp,
                # Only a never-confirmed pending row, and only the secret the
                # code was verified against in phase A (a concurrent setup
                # re-key must not activate a different secret).
                MFAMethod.enabled.is_(False),
                MFAMethod.verified_at.is_(None),
                MFAMethod.secret == expected_secret,
                or_(MFAMethod.locked_until.is_(None), MFAMethod.locked_until <= now),
            )
            .values(
                enabled=True,
                is_primary=True,
                is_active=True,
                verified_at=now,
                failed_attempts=0,
                locked_until=None,
                last_used_at=now,
            )
            .returning(MFAMethod.id)
            .execution_options(synchronize_session=False)
        ).first()
        if activated is None:
            raise _invalid_challenge()
        codes = af.stage_mfa_recovery_codes(db, method_id)
        staged = af.stage_session_issue(
            db,
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            request=request,
            staff_binding=binding,
        )
        _stage_audit(
            db,
            action="auth.login_succeeded",
            principal_type=challenge.principal_type,
            principal_id=challenge.principal_id,
            staff_binding=binding,
            metadata={
                "credential_id": str(challenge.credential_id),
                "credential_version": challenge.credential_version,
                "session_id": staged.session_id,
                "src": challenge.src,
                "mfa": True,
                "enrolled": True,
                "mfa_method_id": str(method_id),
                "audience": challenge.audience,
            },
        )
        return EnrollmentOutcome(staged_session=staged, recovery_codes=tuple(codes))

    context = _context(
        challenge.principal_type,
        challenge.principal_id,
        "Confirm forced MFA enrollment and establish a session",
    )
    try:
        return _run(db, _ENROLLMENT_COMMAND, context, operation)
    except PasswordAuthenticationError as exc:
        if exc.kind == "invalid_mfa_code":
            record_mfa_failure(db, method_id, failure_policy)
        raise


# ---------------------------------------------------------------------------
# 4.4 Failure counters (R5): one atomic statement each
# ---------------------------------------------------------------------------


def password_failure_statement(
    credential_id: UUID,
    *,
    now: datetime,
    policy: FailurePolicy,
):
    """Atomic increment-or-lock for ``user_credentials``.

    The right-hand sides read the OLD row, so parallel wrong guesses cannot
    lose increments, an already-locked account is never extended, and an
    expired lock starts a fresh window of one.
    """

    c = UserCredential
    fresh = case(
        (c.locked_until.is_not(None), 1),
        else_=func.coalesce(c.failed_login_attempts, 0) + 1,
    )
    return (
        update(c)
        .where(c.id == credential_id)
        .values(
            failed_login_attempts=case(
                (c.locked_until > now, c.failed_login_attempts), else_=fresh
            ),
            locked_until=case(
                (c.locked_until > now, c.locked_until),
                (
                    fresh >= policy.max_attempts,
                    now + timedelta(minutes=policy.lock_minutes),
                ),
                else_=None,
            ),
        )
        .returning(c.failed_login_attempts, c.locked_until)
        .execution_options(synchronize_session=False)
    )


def mfa_failure_statement(
    method_id: UUID,
    *,
    now: datetime,
    policy: FailurePolicy,
):
    """Atomic MFA failure update; resets the counter to 0 when the lock is set."""

    m = MFAMethod
    count = func.coalesce(m.failed_attempts, 0) + 1
    return (
        update(m)
        .where(m.id == method_id)
        .values(
            failed_attempts=case(
                (m.locked_until > now, m.failed_attempts),
                (count >= policy.max_attempts, 0),
                else_=count,
            ),
            locked_until=case(
                (m.locked_until > now, m.locked_until),
                (
                    count >= policy.max_attempts,
                    now + timedelta(minutes=policy.lock_minutes),
                ),
                else_=m.locked_until,
            ),
        )
        .returning(m.failed_attempts, m.locked_until)
        .execution_options(synchronize_session=False)
    )


def record_authentication_denial(denial: AuthenticationDenial) -> None:
    """Best-effort failure owner on an independent session.

    A counter and its audit row commit together, or both roll back. The caller's
    session is never inspected or completed, including already-flushed writes.
    Failure evidence cannot grant access or replace the original refusal.
    """
    try:
        from app.metrics import AUTHENTICATION_UNKNOWN_DENIALS

        if denial.target is None:
            AUTHENTICATION_UNKNOWN_DENIALS.inc()
            return
        with db_session_adapter.owner_command_session() as db:

            def operation() -> None:
                _set_lock_timeout(db)
                if isinstance(denial.effect, PasswordFailureEffect):
                    db.execute(
                        password_failure_statement(
                            denial.effect.credential_id,
                            now=_now(),
                            policy=denial.effect.policy,
                        )
                    )
                elif isinstance(denial.effect, MfaFailureEffect):
                    db.execute(
                        mfa_failure_statement(
                            denial.effect.method_id,
                            now=_now(),
                            policy=denial.effect.policy,
                        )
                    )
                assert denial.target is not None
                stage_audit_event(
                    db,
                    action="auth.authentication_denied",
                    entity_type=denial.target.kind.value,
                    entity_id=str(denial.target.identifier),
                    actor=AuditActor.system(OWNER),
                    is_success=False,
                    status_code=denial.status_code,
                    request_id=str(denial.correlation_id),
                    ip_address=str(denial.trusted_ip) if denial.trusted_ip else None,
                    metadata={
                        "schema_version": 1,
                        "reason": denial.reason.value,
                        "surface": denial.surface.value,
                        "phase": denial.phase.value,
                        "target_id": str(denial.target.identifier),
                        "provenance": denial.target.provenance.value,
                        "attempt_id": str(denial.attempt_id),
                        "correlation_id": str(denial.correlation_id),
                    },
                )

            execute_owner_command(
                db,
                definition=_DENIAL_COMMAND,
                context=CommandContext.system(
                    actor=OWNER,
                    scope=SCOPE,
                    reason="Record an authentication denial",
                    command_id=denial.attempt_id,
                    correlation_id=denial.correlation_id,
                ),
                operation=operation,
            )
    except Exception:
        # No exception text/traceback: database exceptions may contain secrets.
        try:
            from app.metrics import AUTHENTICATION_DENIAL_WRITE_FAILURES

            AUTHENTICATION_DENIAL_WRITE_FAILURES.inc()
        except Exception:
            pass
        try:
            logger.warning("authentication_denial_write_failed")
        except Exception:
            pass


def record_password_failure(
    db: Session,
    credential_id: UUID,
    policy: FailurePolicy,
    *,
    principal_type: str = "user",
    principal_id: str = "unknown",
    request: Request | None = None,
    surface: DenialSurface = DenialSurface.LOGIN,
) -> None:
    """Compatibility boundary: the request session is deliberately untouched."""
    record_authentication_denial(
        denial_for_request(
            reason=DenialReason.INVALID_CREDENTIALS,
            surface=surface,
            phase=DenialPhase.PASSWORD,
            target=DenialTarget(DenialTargetKind.CREDENTIAL, credential_id),
            request=request,
            effect=PasswordFailureEffect(credential_id, policy),
        )
    )


def record_mfa_failure(
    db: Session,
    method_id: UUID,
    policy: FailurePolicy,
    *,
    request: Request | None = None,
    surface: DenialSurface = DenialSurface.LOGIN,
) -> None:
    """Compatibility boundary: the request session is deliberately untouched."""
    record_authentication_denial(
        denial_for_request(
            reason=DenialReason.INVALID_CODE,
            surface=surface,
            phase=DenialPhase.MFA,
            target=DenialTarget(DenialTargetKind.METHOD, method_id),
            request=request,
            effect=MfaFailureEffect(method_id, policy),
        )
    )


# ---------------------------------------------------------------------------
# 4.5 Authenticated password change (R7)
# ---------------------------------------------------------------------------


def apply_password_change(
    db: Session,
    *,
    credential_id: UUID,
    expected_version: int,
    new_hash: str,
    principal_type: str,
    principal_id: str,
    session_filter,
    current_session_id: UUID | None,
) -> PasswordChangeOutcome:
    """Phase B of ``change_password``: gate on the version phase A verified.

    ``current_password`` was verified against ``expected_version``; if a reset
    (or any standing change) committed since, the gate returns zero rows and
    the change is refused instead of silently overwriting the reset.
    """

    release_read_transaction(db)

    def operation() -> PasswordChangeOutcome:
        _set_lock_timeout(db)
        now = _now()
        row = db.execute(
            update(UserCredential)
            .where(
                UserCredential.id == credential_id,
                UserCredential.credential_version == expected_version,
                UserCredential.is_active.is_(True),
            )
            .values(
                password_hash=new_hash,
                credential_version=UserCredential.credential_version + 1,
                password_updated_at=now,
                must_change_password=False,
            )
            .returning(UserCredential.credential_version)
            .execution_options(synchronize_session=False)
        ).first()
        if row is None:
            raise PasswordAuthenticationError(
                "credential_changed", "Credential changed; sign in again."
            )
        revoke = update(AuthSession).where(
            session_filter,
            AuthSession.status == SessionStatus.active,
            AuthSession.revoked_at.is_(None),
        )
        if current_session_id is not None:
            revoke = revoke.where(AuthSession.id != current_session_id)
        revoked = db.execute(
            revoke.values(status=SessionStatus.revoked, revoked_at=now)
            .returning(AuthSession.id)
            .execution_options(synchronize_session=False)
        ).all()
        _stage_audit(
            db,
            action="auth.password_changed",
            principal_type=principal_type,
            principal_id=principal_id,
            staff_binding=None,
            metadata={
                "credential_id": str(credential_id),
                "credential_version": int(row[0]),
                "sessions_revoked": len(revoked),
            },
        )
        return PasswordChangeOutcome(
            changed_at=now,
            credential_version=int(row[0]),
            principal_type=principal_type,
            principal_id=principal_id,
            revoked_session_ids=tuple(str(item[0]) for item in revoked),
        )

    return _run(
        db,
        _CHANGE_COMMAND,
        _context(principal_type, principal_id, "Change a password"),
        operation,
    )


# ---------------------------------------------------------------------------
# Representation hook (built, NOT called by login in this change)
# ---------------------------------------------------------------------------


def replace_password_representation(
    db: Session,
    *,
    credential_id: UUID,
    expected_version: int,
    expected_hash: str,
    new_hash: str,
) -> bool:
    """Swap the hash representation of the SAME secret without a version bump.

    Runs in the caller's transaction (it is the B-PR4 upgrade hook point after
    the gate). The transaction-local GUC lets the credential trigger tell a
    representation-only change from a secret change; it is set immediately
    before the single UPDATE and cleared right after. This is the only
    function allowed to name that setting (architecture-tested).
    """

    postgres = _is_postgresql(db)
    if postgres:
        db.execute(
            text(
                "SELECT set_config('app.credential_change_kind', 'representation', true)"
            )
        )
    row = db.execute(
        update(UserCredential)
        .where(
            UserCredential.id == credential_id,
            UserCredential.credential_version == expected_version,
            UserCredential.password_hash == expected_hash,
        )
        .values(password_hash=new_hash)
        .returning(UserCredential.id)
        .execution_options(synchronize_session=False)
    ).first()
    # If the UPDATE raised, the transaction is aborted and the local setting
    # dies with it; reaching here means the statement ran.
    if postgres:
        db.execute(text("SELECT set_config('app.credential_change_kind', '', true)"))
    return row is not None


def revoke_undelivered_customer_session(
    db: Session, *, session_id: UUID, subscriber_id: UUID
) -> None:
    """Compensate a committed issuance whose Redis delivery failed."""
    release_read_transaction(db)

    def operation() -> None:
        _set_lock_timeout(db)
        db.execute(
            update(AuthSession)
            .where(
                AuthSession.id == session_id,
                AuthSession.subscriber_id == subscriber_id,
                AuthSession.status == SessionStatus.active,
            )
            .values(status=SessionStatus.revoked, revoked_at=_now())
        )

    _run(
        db,
        _REVOKE_DELIVERY_COMMAND,
        _context("subscriber", str(subscriber_id), "Revoke undelivered portal session"),
        operation,
    )


def establish_customer_impersonation_session(
    db: Session,
    *,
    subscriber_id: UUID,
    request: Request,
    expires_at: datetime,
) -> StagedSession:
    """Issue standing for an already-authorized impersonation adapter.

    Caller retains its existing staff/reseller authorization and actor audit.
    This session has the same reset and expiry authority as ordinary portal login.
    """
    release_read_transaction(db)

    def operation() -> StagedSession:
        _set_lock_timeout(db)
        _lock_principal_key_share(
            db, principal_type="subscriber", principal_id=str(subscriber_id)
        )
        _check_eligibility(
            db,
            principal_type="subscriber",
            principal_id=str(subscriber_id),
            staff_binding=None,
            audience="general",
            staff_refusal_kind="account_disabled",
        )
        staged = _auth_flow().stage_session_issue(
            db,
            principal_type="subscriber",
            principal_id=str(subscriber_id),
            request=request,
            expires_at=expires_at,
        )
        _stage_audit(
            db,
            action="auth.login_succeeded",
            principal_type="subscriber",
            principal_id=str(subscriber_id),
            staff_binding=None,
            metadata={
                "session_id": staged.session_id,
                "src": "impersonation",
                "mfa": False,
            },
        )
        return staged

    return _run(
        db,
        _IMPERSONATION_COMMAND,
        _context(
            "subscriber",
            str(subscriber_id),
            "Establish authorized customer impersonation",
        ),
        operation,
    )
