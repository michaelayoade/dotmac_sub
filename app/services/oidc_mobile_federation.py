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
* every pinned binding — ceremony, client, redirect, deployment — matches for
  EXACT equality;
* `dotmac_auth_oidc.native.NativeIDTokenVerifier` accepts the assertion, which
  is the whole of: the signature against the pinned issuer's key set, the
  algorithm allowlist applied BEFORE any key is resolved (`none` and the HMAC
  family never reach key resolution), the JWK's own declared `alg` against the
  token's, exact `iss`, `aud` containing the registered client with `azp`
  required when `aud` is multi-valued, `exp`/`nbf`/`iat` with leeway, the
  maximum token age in BOTH directions, and a constant-time comparison of the
  token's `nonce` against this ceremony's stored digest;
* the verified subject has an ACTIVE local credential against the installed
  verifier, and that credential resolves to an eligible staff principal.

## The verifier is a package, and Sub does not second-guess it

Sub owns the ceremony, the bindings, the local identity and the session. It
does not own ID-token verification: `dotmac-auth-oidc` does, Sub holds one
long-lived verifier per registration (`app.services.oidc_mobile_verifier`), and
this module re-checks none of the list above. A local re-check would not be
defence in depth — it would be a second implementation that disagrees silently,
because only one of the two decides. The verifier's refusals arrive as typed
exception CLASSES and are mapped to Sub's closed vocabulary by class alone;
their message text never reaches a log, a metric, an event or a caller.

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

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from dotmac_auth_oidc.errors import (
    ConfigurationError,
    IDTokenError,
    JWKSError,
    NonceMismatchError,
    OIDCError,
    UnsupportedAlgorithmError,
)
from dotmac_auth_oidc.native import NonceBinding
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metrics import (
    OIDC_MOBILE_CEREMONY_CANCELLED,
    OIDC_MOBILE_CEREMONY_COMPLETED,
    OIDC_MOBILE_CEREMONY_STARTED,
    OIDC_MOBILE_EXCHANGE_FAILED,
    OIDC_MOBILE_JWKS_REFRESH_FAILURES,
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
    OIDC_MECHANISM_CODE,
    REQUIRED_CODE_CHALLENGE_METHOD,
    OidcMobileFederationConfig,
    federation_enabled,
    require_federation_config,
)
from app.services.oidc_mobile_verifier import get_verifier
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
        "algorithm_not_allowed",
        "signing_key_unknown",
        "provider_unavailable",
        "assertion_invalid",
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
    """The one persisted nonce form: the verifier's OWN binding digest.

    Deliberately produced by ``NonceBinding`` rather than by a local
    ``hashlib`` call that happens to agree with it today. The exchange
    reconstructs the binding from this stored column with
    ``NonceBinding.from_sha256_hex``, and that constructor accepts exactly 64
    lowercase hex characters — so a digest computed here in any other shape
    would not be a weaker match, it would be a ceremony that can never be
    redeemed at all. Deriving it from the same class that will later validate
    it removes the possibility rather than documenting it.
    """

    return NonceBinding.from_plaintext(nonce).sha256_hex


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
# 5b. Assertion verification — delegated whole to `dotmac-auth-oidc`
# ---------------------------------------------------------------------------


#: Package exception -> Sub refusal category. The package's contract is the
#: exception CLASS, not its message, so this table branches on nothing else:
#: parsing an exception's text would couple Sub's operator vocabulary to a
#: sentence the package is free to reword, and would put library prose one
#: mistake away from a caller-visible detail.
#:
#: The vocabulary is SHORTER than it was, and that is the honest outcome rather
#: than a loss. `signature_invalid`, `issuer_mismatch`, `audience_mismatch`,
#: `authorized_party_mismatch`, `assertion_expired`, `assertion_not_yet_valid`,
#: `assertion_too_old`, `subject_missing`, `nonce_missing` and
#: `malformed_assertion` were ten names for one decision the verifier now makes
#: as a unit: this assertion is not admissible. Keeping ten labels over a
#: distinction Sub can no longer observe would mean either inventing the
#: difference from a message string or emitting a category that never fires —
#: and a declared category nothing emits is exactly what the closed vocabulary
#: exists to forbid. `nonce_missing` in particular was never separable in the
#: first place: an absent nonce and a wrong one are both "not this ceremony's
#: nonce", and the package answers both with `NonceMismatchError`.
def _verification_refusal(exc: OIDCError) -> _Refused:
    """One package exception as one safe category, counted where it belongs.

    Written as a branch per exception with the category spelled out inline,
    rather than as a class-to-string table, deliberately: the closed-vocabulary
    guard finds categories by reading the literal arguments to `_refuse` out of
    this file, so a table would hide five of them from the very check that
    keeps the vocabulary closed. A lookup that is invisible to its own guard is
    not tidier, it is unmonitored.

    Ordered most specific first — `UnsupportedAlgorithmError` and
    `NonceMismatchError` are both `IDTokenError` subclasses, so a broader
    branch above them would swallow the two categories worth distinguishing.
    The final branch is the catch-all for `DiscoveryError` (the well-known
    document could not be fetched or named another issuer), `ConfigurationError`
    (a registration Sub built that the package refuses) and any other
    `OIDCError` (transport). None of them is a decision about the caller, which
    is why they stay apart from `verifier_unavailable` — that one means an
    operator switched Sub's OWN verifier binding off.
    """

    if isinstance(exc, UnsupportedAlgorithmError):
        return _refuse("algorithm_not_allowed")
    if isinstance(exc, NonceMismatchError):
        return _refuse("nonce_mismatch")
    if isinstance(exc, IDTokenError):
        return _refuse("assertion_invalid")
    if isinstance(exc, JWKSError):
        # An availability signal about the identity provider, split out of the
        # refusal counter because it is not a judgement about the assertion.
        OIDC_MOBILE_JWKS_REFRESH_FAILURES.labels(stage="key_set").inc()
        return _refuse("signing_key_unknown")
    OIDC_MOBILE_JWKS_REFRESH_FAILURES.labels(stage="discovery").inc()
    return _refuse("provider_unavailable")


def _verified_subject(
    id_token: str, config: OidcMobileFederationConfig, nonce_hash: str
) -> str:
    """The external subject this assertion proves, or a safe refusal.

    Sub re-checks NOTHING the verifier already decided. The whole list —
    signature against the pinned issuer's key set, the algorithm allowlist
    applied before any key is resolved, the JWK's declared `alg` against the
    token's, exact `iss`, `aud` containing the client and `azp` when `aud` is
    multi-valued, `exp`/`nbf`/`iat` with leeway, the maximum token age in both
    directions, and the constant-time nonce comparison — belongs to
    `NativeIDTokenVerifier`. A second copy here would not be defence in depth;
    it would be the copy that misses the next fix, and the two would disagree
    silently because only one of them decides.

    The nonce binding is reconstructed from the ceremony's STORED DIGEST. Sub
    persists a hash and never the plaintext, and that stays true: the binding
    holds a digest, compares in constant time, and has no accessor that could
    hand a nonce back.
    """

    try:
        binding = NonceBinding.from_sha256_hex(nonce_hash)
    except ConfigurationError:
        # A stored binding the verifier cannot use is a ceremony that can never
        # match any nonce. Reported as a mismatch because that is what it is,
        # and burned for the same reason a real mismatch is.
        raise _refuse("nonce_mismatch") from None

    try:
        verified = get_verifier(config).verify(id_token, nonce_binding=binding)
    except OIDCError as exc:
        raise _verification_refusal(exc) from None
    return verified.subject


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
) -> AdmittedFederatedPrincipal:
    """Burn the ceremony and resolve the principal, under one lock.

    The assertion is verified INSIDE this lock, after the ceremony's own
    cheap checks and before anything is resolved. Two properties fall out of
    that ordering and both are the reason for it:

    * **Every refusal burns the ceremony**, verification refusals included.
      Before the verifier moved into `dotmac-auth-oidc`, verification ran ahead
      of the lock, so a bad signature left the row redeemable and an attacker
      holding an assertion could keep trying against a live ceremony. That was
      an artifact of ordering, not a decision, and it is now closed.
    * **A ceremony that is already refused costs nothing outbound.** Expiry,
      replay and every pinned-binding comparison are settled from local rows
      first, so a caller cannot buy a request to the identity provider with a
      ceremony id that was never going to be admitted.

    Verification is a memory-only operation whenever the held verifier's key
    set is warm, which is the steady state — the verifier is process-lived
    precisely so that stays true. A cold or rotating key set costs one fetch,
    bounded by ``oidc_mobile_jwks_timeout_seconds``, while this row lock is
    held; the contended row is one single-use ceremony belonging to one device,
    so nothing else waits on it.
    """

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

    try:
        subject = _verified_subject(command.id_token, config, ceremony.nonce_hash)
    except _Refused as refused:
        _burn(ceremony, now, OidcCeremonyOutcome.failed, refused.reason)
        raise

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
            return _admit(db, command, config)
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
