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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from jose import JWTError
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.catalog import AccessCredential
from app.services import staff_party_authentication
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from fastapi import Request

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
_ENROLLED_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_MFA, name="establish_enrolled_session"
)
_PASSWORD_FAILURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_FAILURES, name="record_password_failure"
)
_MFA_FAILURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_FAILURES, name="record_mfa_failure"
)
_CHANGE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN_CHANGE, name="change_password"
)

SRC_USER_CREDENTIAL = "user_credential"
SRC_ACCESS_CREDENTIAL = "access_credential"
#: ``radius_user`` (customer-portal RADIUS path without a UserCredential) is a
#: separate follow-up; a challenge claiming it is refused (fail closed).
_SOURCES = frozenset({SRC_USER_CREDENTIAL, SRC_ACCESS_CREDENTIAL})
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


@dataclass(frozen=True)
class FailurePolicy:
    max_attempts: int
    lock_minutes: int


@dataclass(frozen=True)
class PasswordStepOutcome:
    """``kind`` is ``session`` | ``mfa_challenge`` | ``enrollment_challenge``."""

    kind: str
    credential_version: int | None
    staged_session: Any = None
    challenge_token: str | None = None


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

    model = {"system_user": SystemUser, "reseller_user": ResellerUser}.get(
        principal_type, Subscriber
    )
    db.execute(
        select(model.id)
        .where(model.id == coerce_uuid(principal_id))
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
    if verified.src == SRC_ACCESS_CREDENTIAL:
        # R8: PPPoE secret authentication never touches user_credentials. Only
        # confirm the secret that was verified is still the live one.
        row = db.execute(
            select(AccessCredential.id).where(
                AccessCredential.id == verified.access_credential_id,
                AccessCredential.is_active.is_(True),
                AccessCredential.secret_hash == verified.verified_hash,
            )
        ).first()
        if row is None:
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
) -> PasswordStepOutcome:
    """Phase B of the password step: gate, then session OR challenge, + audit."""

    af = _auth_flow()
    release_read_transaction(db)

    def operation() -> PasswordStepOutcome:
        _set_lock_timeout(db)
        now = _now()
        if not (verified.mfa_required or verified.enrollment_required):
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


def _gate_challenge_standing(db: Session, challenge: Challenge, now: datetime) -> None:
    """The credential gate for a challenge. Nothing is consumed on a miss."""

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
            select(AccessCredential.id).where(
                AccessCredential.id == challenge.access_credential_id,
                AccessCredential.is_active.is_(True),
                AccessCredential.updated_at
                == _from_epoch_us(challenge.access_credential_updated_us or 0),
            )
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
):
    """Phase B of MFA completion; returns the ``StagedSession`` (committed).

    Lock order: credential -> mfa_recovery_codes -> mfa_methods -> sessions.
    A wrong second factor rolls everything back, then records the failure in
    its own small owner transaction and re-raises ``invalid_mfa_code``.
    """

    af = _auth_flow()
    release_read_transaction(db)
    binding = _challenge_binding(challenge)

    def operation():
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
    try:
        return _run(db, _MFA_COMMAND, context, operation)
    except PasswordAuthenticationError as exc:
        if exc.kind == "invalid_mfa_code":
            record_mfa_failure(db, method_id, failure_policy)
        raise


def establish_enrolled_session(
    db: Session,
    challenge: Challenge,
    *,
    request: Request,
):
    """Forced-enrollment completion: same credential gate, then session + audit."""

    af = _auth_flow()
    release_read_transaction(db)
    binding = _challenge_binding(challenge)
    if challenge.principal_type != "system_user" or binding is None:
        raise _invalid_challenge()

    def operation():
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
                "audience": challenge.audience,
            },
        )
        return staged

    return _run(
        db,
        _ENROLLED_COMMAND,
        _context(
            challenge.principal_type,
            challenge.principal_id,
            "Establish a session after forced MFA enrollment",
        ),
        operation,
    )


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


def record_password_failure(
    db: Session,
    credential_id: UUID,
    policy: FailurePolicy,
    *,
    principal_type: str = "user",
    principal_id: str = "unknown",
) -> None:
    """Record one wrong password in its own owner transaction.

    A write failure NEVER turns a denial into success: it is logged (no
    identifier, no password) and swallowed, so the caller still answers 401.
    """

    release_read_transaction(db)

    def operation() -> None:
        _set_lock_timeout(db)
        db.execute(password_failure_statement(credential_id, now=_now(), policy=policy))

    try:
        _run(
            db,
            _PASSWORD_FAILURE_COMMAND,
            _context(principal_type, principal_id, "Record a failed password"),
            operation,
        )
    except Exception:
        logger.warning("password_failure_counter_write_failed", exc_info=True)


def record_mfa_failure(db: Session, method_id: UUID, policy: FailurePolicy) -> None:
    """Record one wrong second factor in its own owner transaction (fail-safe)."""

    release_read_transaction(db)

    def operation() -> None:
        _set_lock_timeout(db)
        db.execute(mfa_failure_statement(method_id, now=_now(), policy=policy))

    try:
        _run(
            db,
            _MFA_FAILURE_COMMAND,
            _context("mfa_method", str(method_id), "Record a failed MFA code"),
            operation,
        )
    except Exception:
        logger.warning("mfa_failure_counter_write_failed", exc_info=True)


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
