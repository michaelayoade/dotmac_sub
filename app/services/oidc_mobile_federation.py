"""The one owner of the field-mobile OIDC ceremony and assertion exchange.

Sub remains the SESSION AUTHORITY. The identity provider is a transport that
carries one fact — "this issuer authenticated subject S" — and nothing else it
says has any effect here. Roles, groups, `realm_access`, `resource_access` and
every authorization scope in the assertion are not read, not stored, and not
projected; a technician's permissions come from Sub's own grants and only from
there. That is not a policy this module applies, it is a shape: there is no
code path from a token claim to an authorization decision.

## What each half is for

**Start** hands the device the parameters of a ceremony and an opaque ceremony
id. It creates no user, no session and no credential, and it stores a nonce
HASH rather than the nonce. It never receives the PKCE verifier — the device
generates and keeps that, and exchanges it directly with the identity provider.
Sub never sees the authorization code either.

**Exchange** admits an assertion or refuses it. Admission is the conjunction of
every check below; there is no partial credit and no fallback:

* the ceremony exists, has not expired, and has not been used;
* the signature verifies against the pinned issuer's JWKS;
* the algorithm is asymmetric and exactly allowed (`RS256`) — `none` and the
  HMAC family are refused before a key is even looked up;
* `iss` equals the pinned issuer;
* `aud` contains the configured audience, and `azp` is the configured client
  when `aud` is multi-valued;
* `exp`, `nbf`, and an `iat` that is not implausibly old;
* the token's `nonce` matches the ceremony's stored hash, compared in constant
  time;
* every pinned binding — ceremony, client, redirect, deployment — matches for
  EXACT equality;
* the verified subject has an ACTIVE local credential against the installed
  verifier, and that credential resolves to an eligible staff principal.

## Two transactions, in this order, deliberately

The ceremony is burned and the principal resolved in ONE owner command, which
commits. Only then is the session minted, through
``auth_flow.issue_session_tokens`` — the one issuance owner, which commits its
own session row.

They are not merged, because ``_issue_tokens`` owns its own commit and an owner
command refuses a helper commit inside its boundary. The ordering that falls
out is the safe one: if minting fails after the ceremony committed, the
ceremony is still burned and the device must run a fresh one. The failure mode
is "log in again", never "the assertion is still redeemable".

## Never provisioned, never guessed

An unbound subject is refused. There is no just-in-time provisioning, no
match-by-email, and no second lookup to fall back to — one query, one exact
`(binding, subject)` match on an active row, or a refusal. Binding a subject to
a party is an operator action with its own evidence
(``credential_party_binding``); a login is not the place to invent an identity.

## Refusals say almost nothing

Every refusal category below is safe to expose, and the adapter maps them to
401/403 without distinguishing "no such subject" from "disabled binding" to a
caller. The distinction is real and is recorded for the operator in metrics and
logs; handing it to whoever can drive a login would be a subject-enumeration
oracle. Nothing on this path — log line, metric label, event payload, audit
record or error detail — carries a token, an authorization code, a PKCE
verifier, a nonce, a subject, a key id or an email, truncated or otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request
from jose import jwt
from jose.exceptions import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metrics import (
    OIDC_MOBILE_CEREMONY_CANCELLED,
    OIDC_MOBILE_CEREMONY_COMPLETED,
    OIDC_MOBILE_CEREMONY_STARTED,
    OIDC_MOBILE_EXCHANGE_FAILED,
    OIDC_MOBILE_REPLAY_REFUSED,
    OIDC_MOBILE_UNBOUND_SUBJECT,
)
from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.oidc_mobile import OidcCeremonyOutcome, OidcMobileCeremony
from app.services import auth_flow as auth_flow_service
from app.services import staff_party_authentication
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.authentication_mechanism_registry import require_declared_mechanism
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.oidc_mobile_config import (
    ALLOWED_ID_TOKEN_ALGORITHMS,
    OIDC_MECHANISM_CODE,
    REQUIRED_CODE_CHALLENGE_METHOD,
    OidcMobileFederationConfig,
    federation_enabled,
    require_federation_config,
)
from app.services.oidc_mobile_jwks import signing_key
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OIDC_MOBILE_FEDERATION_SCOPE = "auth:oidc_mobile_federation"

#: The single scope a ceremony requests. Sub reads `sub`, `nonce`, `iss`, `aud`
#: and `azp` and deliberately nothing else, so asking for `profile` or `email`
#: would request data no code here consumes.
CEREMONY_SCOPE = "openid"

#: The concern strings. These are the EXACT names the SOT manifest contracts
#: and the owner-command definitions share; they must not drift apart.
CEREMONY_CONCERN = "field mobile OIDC ceremony lifecycle"
ADMISSION_CONCERN = "field mobile OIDC assertion admission"

_OWNER = "auth.oidc_mobile_federation"

_START_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=CEREMONY_CONCERN,
    name="start_mobile_ceremony",
)
_EXCHANGE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=ADMISSION_CONCERN,
    name="admit_mobile_assertion",
)

#: The CLOSED refusal vocabulary. Every member is safe to log, label a metric
#: with, and hand to an operator. A reason that is not on this list cannot be
#: emitted, which is what keeps a future refusal from carrying a subject or a
#: token fragment "just for debugging".
REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "federation_disabled",
        "unsupported_challenge_method",
        "malformed_assertion",
        "algorithm_not_allowed",
        "signing_key_unknown",
        "signature_invalid",
        "issuer_mismatch",
        "audience_mismatch",
        "authorized_party_mismatch",
        "assertion_expired",
        "assertion_not_yet_valid",
        "assertion_too_old",
        "subject_missing",
        "nonce_missing",
        "nonce_mismatch",
        "ceremony_not_found",
        "ceremony_expired",
        "ceremony_already_used",
        "binding_mismatch",
        "verifier_unavailable",
        "subject_not_bound",
        "principal_not_eligible",
    }
)

#: What the caller is told. One sentence for every refusal above, because the
#: category is for the operator and the message is for the device.
_REFUSAL_MESSAGE = "Federated sign-in was refused."

_REPLAY_REASONS = {"ceremony_already_used", "nonce_mismatch"}


class OidcFederationRefused(DomainError):
    """A ceremony or assertion was refused. `reason` is a safe category."""

    def __init__(self, reason: str) -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"undeclared OIDC refusal category {reason!r}")
        super().__init__(
            code=f"{_OWNER}.refused",
            message=_REFUSAL_MESSAGE,
            details={"reason": reason},
        )
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StartMobileCeremonyCommand:
    """A device asking for the parameters of one federated login."""

    context: CommandContext
    #: Echoed back and refused unless it is `S256`. Sub never receives the
    #: verifier this challenge was derived from, and there is no field for one.
    code_challenge_method: str
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class MobileCeremonyStarted:
    """Everything the device needs, and the nonce it will never send again."""

    ceremony_id: UUID
    issuer: str
    client_id: str
    redirect_uri: str
    audience: str
    scope: str
    nonce: str
    code_challenge_method: str
    expires_at: datetime
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class ExchangeMobileAssertionCommand:
    """A device redeeming one ceremony with one identity-provider assertion."""

    context: CommandContext
    ceremony_id: UUID
    id_token: str
    #: What the device ACTUALLY used, compared for exact equality against what
    #: the ceremony pinned. A device that used a different client or callback
    #: than the ceremony declared is refused rather than reconciled.
    client_id: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AdmittedFederatedPrincipal:
    """The committed admission: who, on which ceremony. No token material."""

    ceremony_id: UUID
    principal_type: str
    principal_id: UUID
    party_id: UUID
    credential_id: UUID


@dataclass(frozen=True, slots=True)
class FederatedSessionIssued:
    """The normal Sub pair, plus the identity it was issued for."""

    access_token: str
    refresh_token: str
    token_type: str
    principal_type: str
    principal_id: UUID
    ceremony_id: UUID


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def nonce_digest(nonce: str) -> str:
    """SHA-256 hex of a nonce. The only form that is ever persisted."""

    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


class _Refused(Exception):  # noqa: N818 - a control signal, not a failure
    """Internal control signal: this ceremony or assertion is refused.

    Deliberately NOT the public error. A refusal raised deep inside an owner
    command would roll the command back — including the write that BURNS the
    ceremony, leaving the row redeemable after a refusal that was supposed to
    consume it. So a refusal is caught at the command boundary, returned as a
    value so the burn commits, and only then reported.
    """

    def __init__(self, reason: str) -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"undeclared OIDC refusal category {reason!r}")
        super().__init__(reason)
        self.reason = reason


def _report(reason: str) -> OidcFederationRefused:
    """Count it, log the safe category, and build the caller's error. Once."""

    OIDC_MOBILE_EXCHANGE_FAILED.labels(reason=reason).inc()
    if reason in _REPLAY_REASONS:
        OIDC_MOBILE_REPLAY_REFUSED.labels(
            kind="ceremony" if reason == "ceremony_already_used" else "nonce"
        ).inc()
    if reason == "subject_not_bound":
        OIDC_MOBILE_UNBOUND_SUBJECT.inc()
    logger.info(
        "oidc_mobile_exchange_refused",
        extra={"event": "oidc_mobile_exchange_refused", "reason": reason},
    )
    return OidcFederationRefused(reason)


def _refuse(reason: str) -> _Refused:
    return _Refused(reason)


# ---------------------------------------------------------------------------
# 5a. Ceremony start
# ---------------------------------------------------------------------------


def start_mobile_ceremony(
    db: Session,
    command: StartMobileCeremonyCommand,
) -> MobileCeremonyStarted:
    """Begin one ceremony. Creates no user, no session and no credential.

    Everything — the flag read, the configuration read, the supersession and
    the insert — happens inside the owner command, because
    ``execute_owner_command`` requires a transaction-free session at entry and
    a settings read is a SELECT that opens one.
    """

    nonce = secrets.token_urlsafe(32)
    digest = nonce_digest(nonce)
    device_id = auth_flow_service._clean_device_id(command.device_id)  # noqa: SLF001

    def operation() -> MobileCeremonyStarted | _Refused:
        if not federation_enabled(db):
            return _refuse("federation_disabled")
        if command.code_challenge_method != REQUIRED_CODE_CHALLENGE_METHOD:
            # `plain` is not a weaker S256, it is the absence of PKCE: the
            # challenge equals the verifier, so an intercepted authorization
            # code is redeemable by whoever intercepted it.
            return _refuse("unsupported_challenge_method")

        config = require_federation_config(db)
        now = _now()
        if device_id:
            # One outstanding ceremony per device. A user who backed out of a
            # sign-in and started again should not leave a redeemable ceremony
            # behind; superseding it here is what makes that count as a
            # cancellation rather than an expiry nobody sees.
            superseded = (
                db.query(OidcMobileCeremony)
                .filter(
                    OidcMobileCeremony.device_id == device_id,
                    OidcMobileCeremony.outcome == OidcCeremonyOutcome.pending.value,
                )
                .update(
                    {
                        OidcMobileCeremony.outcome: (
                            OidcCeremonyOutcome.cancelled.value
                        ),
                        OidcMobileCeremony.consumed_at: now,
                        OidcMobileCeremony.failure_reason: "superseded_by_new_start",
                    },
                    synchronize_session=False,
                )
            )
            if superseded:
                OIDC_MOBILE_CEREMONY_CANCELLED.inc(superseded)

        ceremony = OidcMobileCeremony(
            id=uuid.uuid4(),
            binding_key=config.binding_key,
            issuer=config.issuer,
            client_id=config.client_id,
            redirect_uri=config.redirect_uri,
            deployment_id=config.deployment_id,
            nonce_hash=digest,
            device_id=device_id,
            created_at=now,
            expires_at=now + timedelta(seconds=config.ceremony_ttl_seconds),
            outcome=OidcCeremonyOutcome.pending.value,
        )
        db.add(ceremony)
        db.flush()
        emit_event(
            db,
            EventType.oidc_mobile_ceremony_started,
            {
                "ceremony_id": str(ceremony.id),
                "binding_key": ceremony.binding_key,
                "deployment_id": ceremony.deployment_id,
            },
            actor=command.context.actor,
        )
        # Build the public value while the owner transaction is still open.
        # Returning an ORM row here made the post-commit read below trigger
        # SQLAlchemy's expire-on-commit refresh, silently opening a caller
        # transaction that the immediately-following exchange must refuse.
        return MobileCeremonyStarted(
            ceremony_id=ceremony.id,
            issuer=config.issuer,
            client_id=config.client_id,
            redirect_uri=config.redirect_uri,
            audience=config.audience,
            scope=CEREMONY_SCOPE,
            nonce=nonce,
            code_challenge_method=REQUIRED_CODE_CHALLENGE_METHOD,
            expires_at=_as_utc(ceremony.expires_at) or now,
            expires_in_seconds=config.ceremony_ttl_seconds,
        )

    result = execute_owner_command(
        db,
        definition=_START_COMMAND,
        context=command.context,
        operation=operation,
    )
    if isinstance(result, _Refused):
        raise _report(result.reason)

    OIDC_MOBILE_CEREMONY_STARTED.inc()
    logger.info(
        "oidc_mobile_ceremony_started",
        extra={
            "event": "oidc_mobile_ceremony_started",
            "ceremony_id": str(result.ceremony_id),
        },
    )
    return result


# ---------------------------------------------------------------------------
# 5b. Assertion verification (no database writes happen in here)
# ---------------------------------------------------------------------------


def _verified_claims(
    id_token: str, config: OidcMobileFederationConfig
) -> dict[str, Any]:
    """Verify the assertion completely, or raise a safe refusal.

    The algorithm is checked from the HEADER before any key is fetched. Doing
    it here rather than relying on the JWT library's `algorithms` argument
    alone gives a precise refusal category and, more importantly, means an
    `alg: none` token never reaches the key-resolution path at all — so it
    cannot even cost an outbound request.
    """

    try:
        header = jwt.get_unverified_header(id_token)
    except (JWTError, AttributeError, ValueError):
        raise _refuse("malformed_assertion") from None

    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in ALLOWED_ID_TOKEN_ALGORITHMS:
        raise _refuse("algorithm_not_allowed")

    key = signing_key(config, header.get("kid"))
    if key is None:
        raise _refuse("signing_key_unknown")

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=sorted(ALLOWED_ID_TOKEN_ALGORITHMS),
            audience=config.audience,
            issuer=config.issuer,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_at_hash": False,
                "require_aud": True,
                "require_exp": True,
                "require_iat": True,
                "require_iss": True,
                "require_sub": True,
                "leeway": config.clock_skew_seconds,
            },
        )
    except JWTError as exc:
        # The library reports every claim failure as one error type, so the
        # category is derived from its text. Mapping it here keeps the closed
        # vocabulary closed; anything unrecognised degrades to
        # `signature_invalid`, which is the safe direction.
        text = str(exc).lower()
        if "expire" in text:
            raise _refuse("assertion_expired") from None
        if "not yet valid" in text or "nbf" in text:
            raise _refuse("assertion_not_yet_valid") from None
        if "audience" in text:
            raise _refuse("audience_mismatch") from None
        if "issuer" in text:
            raise _refuse("issuer_mismatch") from None
        if "signature" in text:
            raise _refuse("signature_invalid") from None
        raise _refuse("malformed_assertion") from None

    if claims.get("iss") != config.issuer:
        # Belt and braces: the library already compared it, and an issuer this
        # deployment does not trust is the one claim worth checking twice.
        raise _refuse("issuer_mismatch")

    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else list(audience or ())
    if config.audience not in audiences:
        raise _refuse("audience_mismatch")
    if len(audiences) > 1:
        # A multi-valued audience means the token was minted for more than one
        # recipient, and `azp` is the only claim that says which party actually
        # requested it. Without this check, an assertion issued to a different
        # client that happens to list our audience would be admitted.
        if claims.get("azp") != config.client_id:
            raise _refuse("authorized_party_mismatch")

    issued_at = claims.get("iat")
    if not isinstance(issued_at, int):
        raise _refuse("malformed_assertion")
    age = _now().timestamp() - issued_at
    if age > config.max_assertion_age_seconds + config.clock_skew_seconds:
        # `exp` alone is the identity provider's opinion of freshness. An
        # assertion minted long before this exchange is not a live login even
        # if it has not expired yet.
        raise _refuse("assertion_too_old")
    if age < -config.clock_skew_seconds:
        raise _refuse("assertion_not_yet_valid")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise _refuse("subject_missing")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not nonce.strip():
        raise _refuse("nonce_missing")
    return claims


# ---------------------------------------------------------------------------
# 5b. Admission (the owner command that burns the ceremony)
# ---------------------------------------------------------------------------


def _matching_credential(
    db: Session, *, binding: AuthenticationBinding, subject: str
) -> UserCredential | None:
    """The ONE active credential for this subject at this verifier, or None.

    One query, one exact match, locked for update. There is no second lookup
    and in particular no match-by-email: an email is a routing address the
    identity provider may reassign, and treating it as an identity is how one
    person inherits another's account.

    A multi-row result is impossible under
    ``ux_user_credentials_external_subject`` and is still refused here, because
    a resolver that would pick arbitrarily if the index were ever dropped is a
    resolver that picks arbitrarily.
    """

    rows = (
        db.scalars(
            select(UserCredential)
            .where(UserCredential.authentication_binding_id == binding.id)
            .where(UserCredential.username == subject)
            .where(UserCredential.is_active.is_(True))
            .where(UserCredential.provider != AuthProvider.local)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .unique()
        .all()
    )
    if len(rows) != 1:
        return None
    return rows[0]


def _admit(
    db: Session,
    command: ExchangeMobileAssertionCommand,
    config: OidcMobileFederationConfig,
    subject: str,
    nonce: str,
) -> AdmittedFederatedPrincipal:
    """Burn the ceremony and resolve the principal, under one lock."""

    now = _now()
    ceremony = db.scalars(
        select(OidcMobileCeremony)
        .where(OidcMobileCeremony.id == command.ceremony_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if ceremony is None:
        raise _refuse("ceremony_not_found")
    # Under the lock, and only now, is `consumed_at` an answer rather than an
    # observation: no concurrent exchange can burn this row until we commit.
    if ceremony.consumed_at is not None:
        raise _refuse("ceremony_already_used")
    expires_at = _as_utc(ceremony.expires_at)
    if expires_at is None or expires_at <= now:
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "ceremony_expired")
        raise _refuse("ceremony_expired")

    # Constant time: a byte-by-byte comparison of the digest leaks how much of
    # a guessed nonce was right, and the nonce is the anti-replay binding.
    if not hmac.compare_digest(ceremony.nonce_hash, nonce_digest(nonce)):
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "nonce_mismatch")
        raise _refuse("nonce_mismatch")

    # EXACT equality on every pinned binding. No prefix match, no wildcard, no
    # trailing-slash tolerance, no scheme coercion, no case folding — a loose
    # comparison here is what would quietly undo the verified-redirect
    # guarantee the whole ceremony exists to provide.
    if (
        ceremony.issuer != config.issuer
        or ceremony.client_id != config.client_id
        or ceremony.client_id != command.client_id
        or ceremony.redirect_uri != config.redirect_uri
        or ceremony.redirect_uri != command.redirect_uri
        or ceremony.deployment_id != config.deployment_id
        or ceremony.binding_key != config.binding_key
    ):
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "binding_mismatch")
        raise _refuse("binding_mismatch")

    binding = db.scalars(
        select(AuthenticationBinding)
        .where(AuthenticationBinding.binding_key == ceremony.binding_key)
        .where(
            AuthenticationBinding.mechanism_code
            == require_declared_mechanism(OIDC_MECHANISM_CODE)
        )
        .where(AuthenticationBinding.is_active.is_(True))
    ).first()
    if binding is None:
        # The verifier was uninstalled or deactivated. A disabled client cannot
        # authenticate, and the ceremony is burned rather than left redeemable
        # against a binding that might be re-enabled later.
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "verifier_unavailable")
        raise _refuse("verifier_unavailable")

    credential = _matching_credential(db, binding=binding, subject=subject)
    if credential is None or credential.party_id is None:
        # Refused, never provisioned. Binding a subject to a party is an
        # operator action with its own evidence; a login does not invent one.
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "subject_not_bound")
        raise _refuse("subject_not_bound")
    if credential.system_user_id is None:
        # Field federation admits staff principals only. A subscriber or
        # reseller credential bound to an OIDC verifier is a configuration
        # error, and guessing which session to mint for it is exactly the
        # ambiguity that must fail closed.
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "principal_not_eligible")
        raise _refuse("principal_not_eligible")

    try:
        principal = staff_party_authentication.resolve_staff_principal_by_party(
            db,
            credential.party_id,
            credential.system_user_id,
            reference=credential.id,
        )
    except staff_party_authentication.StaffProjectionError:
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "principal_not_eligible")
        raise _refuse("principal_not_eligible") from None
    if not principal.is_active:
        _burn(ceremony, now, OidcCeremonyOutcome.failed, "principal_not_eligible")
        raise _refuse("principal_not_eligible")

    credential.last_login_at = now
    credential.failed_login_attempts = 0
    credential.locked_until = None
    _burn(ceremony, now, OidcCeremonyOutcome.completed, None)

    stage_audit_event(
        db,
        action="auth.oidc_mobile_assertion_admitted",
        entity_type="oidc_mobile_ceremony",
        entity_id=str(ceremony.id),
        actor=AuditActor.user(
            str(principal.id),
            label=command.context.actor,
            party_id=credential.party_id,
        ),
        metadata={
            "binding_key": ceremony.binding_key,
            "deployment_id": ceremony.deployment_id,
            "principal_type": "system_user",
        },
        occurred_at=now,
    )
    emit_event(
        db,
        EventType.oidc_mobile_assertion_admitted,
        {
            "ceremony_id": str(ceremony.id),
            "binding_key": ceremony.binding_key,
            "deployment_id": ceremony.deployment_id,
            "principal_type": "system_user",
            "principal_id": str(principal.id),
        },
        actor=command.context.actor,
    )
    db.flush()
    return AdmittedFederatedPrincipal(
        ceremony_id=ceremony.id,
        principal_type="system_user",
        principal_id=principal.id,
        party_id=credential.party_id,
        credential_id=credential.id,
    )


def _burn(
    ceremony: OidcMobileCeremony,
    now: datetime,
    outcome: OidcCeremonyOutcome,
    reason: str | None,
) -> None:
    """Move a ceremony to its terminal state. Single use, once, here.

    A refused exchange burns the ceremony too. Leaving it redeemable would let
    an attacker who has an assertion but not the nonce keep trying, and would
    let a caller who guessed a ceremony id retry against a live row.
    """

    ceremony.consumed_at = now
    ceremony.outcome = outcome.value
    ceremony.failure_reason = reason


def exchange_mobile_assertion(
    db: Session,
    command: ExchangeMobileAssertionCommand,
    *,
    request: Request,
) -> FederatedSessionIssued:
    """Admit one assertion and return the normal Sub access/refresh pair.

    The configuration read, the assertion verification and the admission all
    run inside ONE owner command, for the same reason the start does: a
    settings read opens a transaction, and ``execute_owner_command`` requires a
    transaction-free session at entry.

    A refusal comes back as a VALUE rather than an exception so the command
    still commits — the ceremony must stay burned after a refused exchange, and
    an exception would roll that back and leave the row redeemable.

    The session is minted afterwards, outside the command, by the one issuance
    owner: ``_issue_tokens`` commits its own session row, and an owner command
    refuses a helper commit inside its boundary.
    """

    def operation() -> AdmittedFederatedPrincipal | _Refused:
        try:
            if not federation_enabled(db):
                return _refuse("federation_disabled")
            config = require_federation_config(db)
            claims = _verified_claims(command.id_token, config)
            return _admit(
                db,
                command,
                config,
                str(claims["sub"]).strip(),
                str(claims["nonce"]).strip(),
            )
        except _Refused as refused:
            return refused

    result = execute_owner_command(
        db,
        definition=_EXCHANGE_COMMAND,
        context=command.context,
        operation=operation,
    )
    if isinstance(result, _Refused):
        raise _report(result.reason)

    # The ceremony is committed and burned. Only now is a session minted, by
    # the ONE issuance owner — never a second one written here.
    tokens = auth_flow_service.issue_session_tokens(
        db,
        principal_type=result.principal_type,
        principal_id=str(result.principal_id),
        request=request,
        staff_binding=staff_party_authentication.StaffSessionBinding(
            party_id=result.party_id,
            system_user_id=result.principal_id,
        ),
    )
    OIDC_MOBILE_CEREMONY_COMPLETED.inc()
    logger.info(
        "oidc_mobile_ceremony_completed",
        extra={
            "event": "oidc_mobile_ceremony_completed",
            "ceremony_id": str(result.ceremony_id),
            "principal_type": result.principal_type,
        },
    )
    return FederatedSessionIssued(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type="bearer",  # noqa: S106 - OAuth token type label, not a credential
        principal_type=result.principal_type,
        principal_id=result.principal_id,
        ceremony_id=result.ceremony_id,
    )
