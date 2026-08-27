"""Behaviour of the Sub-owned field-mobile OIDC seam.

Every test drives the real owner against real rows and real RS256 signatures.
Nothing here mocks the verifier: a mocked call proves the caller said the right
words, and what has to be proven is which key actually verified, which column
actually refused the replay, and which claims actually had no effect.

The negative cases are the substance. A suite that only ever sends a valid
assertion cannot tell a working verifier from a function that returns True.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.models.auth import AuthProvider, UserCredential
from app.models.oidc_mobile import OidcCeremonyOutcome, OidcMobileCeremony
from app.services import oidc_mobile_federation as federation
from app.services import oidc_mobile_jwks as jwks
from app.services.oidc_mobile_config import (
    ALLOWED_ID_TOKEN_ALGORITHMS,
    OidcFederationConfigError,
    require_federation_config,
)
from app.services.owner_commands import CommandContext
from tests.oidc_mobile_fixtures import (
    AUDIENCE,
    CLIENT_ID,
    ISSUER,
    REDIRECT_URI,
    RecordingTransport,
    SigningKey,
    assertion_claims,
    bind_field_technician,
    configure_federation,
    install_oidc_binding,
)


@pytest.fixture()
def signing_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture()
def transport(signing_key: SigningKey):
    recording = RecordingTransport([signing_key.public_jwk])
    previous = jwks.install_transport(recording)
    jwks.reset_cache()
    try:
        yield recording
    finally:
        jwks.install_transport(previous)
        jwks.reset_cache()


def _context(reason: str = "test") -> CommandContext:
    return CommandContext.system(
        actor="test:oidc",
        scope=federation.OIDC_MOBILE_FEDERATION_SCOPE,
        reason=reason,
    )


def _request(device_id: str | None = None) -> Request:
    headers = [(b"user-agent", b"oidc-federation-test")]
    if device_id is not None:
        headers.append((b"x-device-id", device_id.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/oidc/mobile/exchange",
            "headers": headers,
            "client": ("127.0.0.1", 40001),
        }
    )


def _start(db_session, *, device_id: str | None = None, method: str = "S256"):
    return federation.start_mobile_ceremony(
        db_session,
        federation.StartMobileCeremonyCommand(
            context=_context("start"),
            code_challenge_method=method,
            device_id=device_id,
        ),
    )


def _exchange(
    db_session,
    ceremony_id,
    id_token: str,
    *,
    client_id: str = CLIENT_ID,
    redirect_uri: str = REDIRECT_URI,
):
    return federation.exchange_mobile_assertion(
        db_session,
        federation.ExchangeMobileAssertionCommand(
            context=_context("exchange"),
            ceremony_id=ceremony_id,
            id_token=id_token,
            client_id=client_id,
            redirect_uri=redirect_uri,
        ),
        request=_request(),
    )


def _refusal(exc_info) -> str:
    return exc_info.value.reason


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------


def test_a_valid_s256_ceremony_exchanges_for_a_sub_session(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    user, _person, _credential = bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session, device_id="device-a")
    assert started.code_challenge_method == "S256"
    assert started.issuer == ISSUER
    assert started.client_id == CLIENT_ID
    assert started.redirect_uri == REDIRECT_URI
    assert started.audience == AUDIENCE
    assert started.nonce

    token = signing_key.sign(assertion_claims(started.nonce))
    issued = _exchange(db_session, started.ceremony_id, token)

    assert issued.access_token
    assert issued.refresh_token
    assert issued.token_type == "bearer"
    assert issued.principal_type == "system_user"
    assert issued.principal_id == user.id


def test_the_raw_nonce_is_never_stored(db_session, signing_key, transport):
    """A stored nonce would let read access mint a replay-satisfying assertion."""

    configure_federation(db_session)
    started = _start(db_session)

    row = db_session.get(OidcMobileCeremony, started.ceremony_id)
    assert row is not None
    assert row.nonce_hash == federation.nonce_digest(started.nonce)
    assert started.nonce not in row.nonce_hash
    stored = {
        column: getattr(row, column)
        for column in (
            "binding_key",
            "issuer",
            "client_id",
            "redirect_uri",
            "deployment_id",
            "nonce_hash",
            "device_id",
            "failure_reason",
        )
    }
    assert started.nonce not in {value for value in stored.values() if value}


def test_starting_a_ceremony_creates_no_user_and_no_session(db_session, transport):
    configure_federation(db_session)
    from app.models.auth import Session as AuthSession
    from app.models.system_user import SystemUser

    users_before = db_session.query(SystemUser).count()
    sessions_before = db_session.query(AuthSession).count()
    credentials_before = db_session.query(UserCredential).count()

    _start(db_session)

    assert db_session.query(SystemUser).count() == users_before
    assert db_session.query(AuthSession).count() == sessions_before
    assert db_session.query(UserCredential).count() == credentials_before


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_plain_pkce_is_refused_at_ceremony_start(db_session, transport):
    """`plain` is not a weaker S256; the challenge equals the verifier."""

    configure_federation(db_session)
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _start(db_session, method="plain")
    assert _refusal(exc) == "unsupported_challenge_method"
    assert db_session.query(OidcMobileCeremony).count() == 0


def test_the_wire_contract_has_nowhere_to_put_a_verifier():
    """The guarantee is a schema, not a review comment."""

    from pydantic import ValidationError

    from app.schemas.oidc_mobile import (
        OidcMobileExchangeRequest,
        OidcMobileStartRequest,
    )

    assert "code_verifier" not in OidcMobileStartRequest.model_fields
    assert "code_verifier" not in OidcMobileExchangeRequest.model_fields

    with pytest.raises(ValidationError):
        OidcMobileStartRequest(code_challenge_method="S256", code_verifier="secret")
    with pytest.raises(ValidationError):
        OidcMobileExchangeRequest(
            ceremony_id=uuid4(),
            id_token="x",
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="secret",
        )


# ---------------------------------------------------------------------------
# Signature, algorithm and claim verification
# ---------------------------------------------------------------------------


def test_only_rs256_is_accepted():
    assert ALLOWED_ID_TOKEN_ALGORITHMS == frozenset({"RS256"})


def test_an_unsigned_assertion_is_refused(db_session, signing_key, transport):
    """`alg: none` must never reach key resolution, let alone verification."""

    configure_federation(db_session)
    started = _start(db_session)
    from jose import jws

    token = jws.sign(assertion_claims(started.nonce), key="", algorithm="none")

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "algorithm_not_allowed"
    # It cost nothing outbound: refusing before key resolution is the point.
    assert transport.calls == []


def test_an_hmac_signed_assertion_is_refused(db_session, transport):
    """A symmetric algorithm substitutes a key the attacker may already hold."""

    configure_federation(db_session)
    started = _start(db_session)
    from jose import jwt as jose_jwt

    token = jose_jwt.encode(
        assertion_claims(started.nonce), "shared-secret", algorithm="HS256"
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "algorithm_not_allowed"
    assert transport.calls == []


def test_a_signature_from_the_wrong_key_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    impostor = SigningKey.generate(kid=signing_key.kid)
    token = impostor.sign(assertion_claims(started.nonce))

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "signature_invalid"


def test_a_wrong_issuer_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    token = signing_key.sign(
        assertion_claims(started.nonce, issuer="https://evil.test.invalid/realms/field")
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "issuer_mismatch"


def test_a_wrong_audience_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    token = signing_key.sign(
        assertion_claims(started.nonce, audience="io.dotmac.someone-else")
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "audience_mismatch"


def test_a_multi_valued_audience_requires_the_right_authorized_party(
    db_session, signing_key, transport
):
    """Without `azp`, a token minted for another client that merely LISTS our
    audience would be admitted."""

    configure_federation(db_session)
    started = _start(db_session)
    token = signing_key.sign(
        assertion_claims(
            started.nonce,
            audience=[AUDIENCE, "io.dotmac.other"],
            azp="some-other-client",
        )
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "authorized_party_mismatch"


def test_a_multi_valued_audience_with_the_right_azp_is_admitted(
    db_session, signing_key, transport
):
    """The sensitivity half: the check above must refuse for the RIGHT reason."""

    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(
        assertion_claims(
            started.nonce,
            audience=[AUDIENCE, "io.dotmac.other"],
            azp=CLIENT_ID,
        )
    )

    assert _exchange(db_session, started.ceremony_id, token).access_token


def test_an_expired_assertion_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    now = int(time.time())
    token = signing_key.sign(
        assertion_claims(started.nonce, issued_at=now - 3600, expires_at=now - 1800)
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) in {"assertion_expired", "assertion_too_old"}


def test_a_not_yet_valid_assertion_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    now = int(time.time())
    token = signing_key.sign(
        assertion_claims(
            started.nonce,
            issued_at=now + 3600,
            not_before=now + 3600,
            expires_at=now + 7200,
        )
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "assertion_not_yet_valid"


def test_an_unexpired_but_stale_assertion_is_refused(
    db_session, signing_key, transport
):
    """`exp` is the identity provider's opinion of freshness; this is Sub's."""

    configure_federation(db_session)
    started = _start(db_session)
    now = int(time.time())
    token = signing_key.sign(
        assertion_claims(started.nonce, issued_at=now - 7200, expires_at=now + 7200)
    )

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "assertion_too_old"


# ---------------------------------------------------------------------------
# Nonce, ceremony and binding
# ---------------------------------------------------------------------------


def test_a_nonce_from_another_ceremony_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    first = _start(db_session, device_id="device-a")
    second = _start(db_session, device_id="device-b")
    token = signing_key.sign(assertion_claims(first.nonce))

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, second.ceremony_id, token)
    assert _refusal(exc) == "nonce_mismatch"


def test_an_unknown_ceremony_is_refused(db_session, signing_key, transport):
    configure_federation(db_session)
    token = signing_key.sign(assertion_claims("some-nonce"))

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, uuid4(), token)
    assert _refusal(exc) == "ceremony_not_found"


def test_an_expired_ceremony_is_refused_and_burned(db_session, signing_key, transport):
    configure_federation(db_session)
    started = _start(db_session)
    row = db_session.get(OidcMobileCeremony, started.ceremony_id)
    row.expires_at = row.created_at
    db_session.commit()

    token = signing_key.sign(assertion_claims(started.nonce))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "ceremony_expired"

    db_session.expire_all()
    row = db_session.get(OidcMobileCeremony, started.ceremony_id)
    assert row.consumed_at is not None
    assert row.outcome == OidcCeremonyOutcome.failed.value


def test_a_ceremony_cannot_be_replayed(db_session, signing_key, transport):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    assert _exchange(db_session, started.ceremony_id, token).access_token

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "ceremony_already_used"


def test_a_refused_exchange_still_burns_the_ceremony(
    db_session, signing_key, transport
):
    """The refusal must COMMIT the burn. If it rolled back — the obvious way to
    write this — a refused exchange would leave the row redeemable and an
    attacker could keep trying against a live ceremony."""

    configure_federation(db_session)
    started = _start(db_session)
    good_token = signing_key.sign(assertion_claims(started.nonce))

    # Refuse on the nonce, which is checked after the ceremony is locked.
    other = _start(db_session, device_id="device-z")
    bad = signing_key.sign(assertion_claims(other.nonce))
    with pytest.raises(federation.OidcFederationRefused):
        _exchange(db_session, started.ceremony_id, bad)

    db_session.expire_all()
    row = db_session.get(OidcMobileCeremony, started.ceremony_id)
    assert row.consumed_at is not None
    assert row.failure_reason == "nonce_mismatch"

    # And the now-burned ceremony refuses even a correct assertion.
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, good_token)
    assert _refusal(exc) == "ceremony_already_used"


def test_a_redirect_uri_the_device_did_not_use_is_refused(
    db_session, signing_key, transport
):
    """Exact equality. No prefix, wildcard, trailing slash or scheme coercion."""

    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding)
    db_session.commit()

    for variant in (
        REDIRECT_URI + "/",
        REDIRECT_URI.replace("https://", "http://"),
        REDIRECT_URI.upper(),
        REDIRECT_URI + "?x=1",
        REDIRECT_URI.rsplit("/", 1)[0],
    ):
        started = _start(db_session)
        token = signing_key.sign(assertion_claims(started.nonce))
        with pytest.raises(federation.OidcFederationRefused) as exc:
            _exchange(db_session, started.ceremony_id, token, redirect_uri=variant)
        assert _refusal(exc) == "binding_mismatch", variant


def test_a_client_id_the_device_did_not_use_is_refused(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token, client_id="other-client")
    assert _refusal(exc) == "binding_mismatch"


def test_configuration_changed_under_an_outstanding_ceremony_is_refused(
    db_session, signing_key, transport
):
    """A pinned binding is only worth pinning if a later change refuses it."""

    configure_federation(db_session)
    started = _start(db_session)
    row = db_session.get(OidcMobileCeremony, started.ceremony_id)
    row.deployment_id = "a-different-deployment"
    db_session.commit()

    token = signing_key.sign(assertion_claims(started.nonce))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "binding_mismatch"


def test_a_new_start_supersedes_that_devices_outstanding_ceremony(
    db_session, transport
):
    configure_federation(db_session)
    first = _start(db_session, device_id="device-a")
    _start(db_session, device_id="device-a")

    db_session.expire_all()
    row = db_session.get(OidcMobileCeremony, first.ceremony_id)
    assert row.outcome == OidcCeremonyOutcome.cancelled.value
    assert row.failure_reason == "superseded_by_new_start"


# ---------------------------------------------------------------------------
# Binding to a local identity — never provisioning one
# ---------------------------------------------------------------------------


def test_an_unbound_subject_is_refused_and_provisions_nothing(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    install_oidc_binding(db_session)
    db_session.commit()

    from app.models.party import Party
    from app.models.system_user import SystemUser

    users_before = db_session.query(SystemUser).count()
    parties_before = db_session.query(Party).count()
    credentials_before = db_session.query(UserCredential).count()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce, subject="stranger"))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "subject_not_bound"

    db_session.expire_all()
    assert db_session.query(SystemUser).count() == users_before
    assert db_session.query(Party).count() == parties_before
    assert db_session.query(UserCredential).count() == credentials_before


def test_a_deactivated_client_cannot_authenticate(db_session, signing_key, transport):
    """Deactivating the installed verifier disables federated sign-in."""

    configure_federation(db_session)
    binding = install_oidc_binding(db_session, is_active=False)
    bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "verifier_unavailable"


def test_a_deactivated_credential_cannot_authenticate(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding, is_active=False)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "subject_not_bound"


def test_a_deactivated_staff_principal_cannot_authenticate(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding, staff_active=False)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, started.ceremony_id, token)
    assert _refusal(exc) == "principal_not_eligible"


def test_external_roles_groups_and_scopes_have_no_authorization_effect(
    db_session, signing_key, transport
):
    """The assertion claims `admin`, `superuser`, `billing:write` and a
    supervisors group. None of them may reach Sub's authorization."""

    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    user, _person, _credential = bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    issued = _exchange(db_session, started.ceremony_id, token)

    from app.services.auth_flow import decode_access_token

    claims = decode_access_token(db_session, issued.access_token)
    assert claims["principal_id"] == str(user.id)
    assert "admin" not in claims.get("roles", [])
    assert "superuser" not in claims.get("roles", [])
    assert "billing:write" not in claims.get("scopes", [])
    assert "groups" not in claims

    from app.models.rbac import SystemUserRole

    granted = db_session.query(SystemUserRole).filter_by(system_user_id=user.id).count()
    assert granted == 0


def test_a_federated_credential_cannot_be_used_for_password_login(
    db_session, signing_key, transport
):
    """A federated credential has no password and must not be reachable through
    the password path at all."""

    from fastapi import HTTPException

    from app.services.auth_flow import AuthFlow

    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding, subject="keycloak-subject-1")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(
            db_session,
            "keycloak-subject-1",
            "",
            _request(),
            AuthProvider.sso.value,
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Bounded JWKS refresh
# ---------------------------------------------------------------------------


def test_an_unknown_key_triggers_exactly_one_bounded_refresh(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    assert _exchange(db_session, started.ceremony_id, token).access_token
    assert len(transport.calls) == 1

    # A second exchange with a KNOWN kid must not refresh again.
    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    assert _exchange(db_session, started.ceremony_id, token).access_token
    assert len(transport.calls) == 1


def test_an_unknown_kid_flood_cannot_amplify_into_outbound_requests(
    db_session, signing_key, transport
):
    """The bound that matters: the caller CHOOSES the kid, so an unrecognised
    one must not buy a request to the identity provider each time."""

    configure_federation(db_session)
    for index in range(6):
        started = _start(db_session)
        token = signing_key.sign(
            assertion_claims(started.nonce), kid=f"rotated-{index}"
        )
        with pytest.raises(federation.OidcFederationRefused) as exc:
            _exchange(db_session, started.ceremony_id, token)
        assert _refusal(exc) == "signing_key_unknown"

    assert len(transport.calls) == 1


def test_a_failed_refresh_does_not_discard_the_working_key_set(
    db_session, signing_key, transport
):
    configure_federation(db_session)
    binding = install_oidc_binding(db_session)
    bind_field_technician(db_session, binding)
    db_session.commit()

    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    assert _exchange(db_session, started.ceremony_id, token).access_token

    transport.fail = True
    started = _start(db_session)
    token = signing_key.sign(assertion_claims(started.nonce))
    assert _exchange(db_session, started.ceremony_id, token).access_token


def test_an_absent_kid_never_costs_an_outbound_request(db_session, signing_key):
    from app.services.oidc_mobile_config import OidcMobileFederationConfig

    recording = RecordingTransport([signing_key.public_jwk])
    previous = jwks.install_transport(recording)
    jwks.reset_cache()
    try:
        config = OidcMobileFederationConfig(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            audience=AUDIENCE,
            binding_key="k",
            deployment_id="d",
            jwks_source="static_uri",
            jwks_uri="https://idp.test.invalid/certs",
            jwks_min_refresh_seconds=300,
            jwks_timeout_seconds=5,
            ceremony_ttl_seconds=300,
            clock_skew_seconds=60,
            max_assertion_age_seconds=300,
        )
        assert jwks.signing_key(config, None) is None
        assert jwks.signing_key(config, "") is None
        assert recording.calls == []
    finally:
        jwks.install_transport(previous)
        jwks.reset_cache()


# ---------------------------------------------------------------------------
# Configuration and the enablement flag
# ---------------------------------------------------------------------------


def test_the_mechanism_is_off_by_default(db_session, transport):
    from app.services.operator_tenant import provision_operator_tenant

    provision_operator_tenant(db_session)
    db_session.commit()

    with pytest.raises(federation.OidcFederationRefused) as exc:
        _start(db_session)
    assert _refusal(exc) == "federation_disabled"


def test_a_disabled_mechanism_refuses_the_exchange_too(db_session, transport):
    configure_federation(db_session, enabled=False)
    with pytest.raises(federation.OidcFederationRefused) as exc:
        _exchange(db_session, uuid4(), "not-a-token")
    assert _refusal(exc) == "federation_disabled"


def test_incomplete_configuration_names_every_missing_key(db_session, transport):
    configure_federation(
        db_session,
        overrides={"oidc_mobile_issuer": "", "oidc_mobile_redirect_uri": ""},
    )
    with pytest.raises(OidcFederationConfigError) as exc:
        _start(db_session)
    missing = exc.value.details["missing_settings"]
    assert "oidc_mobile_issuer" in missing
    assert "oidc_mobile_redirect_uri" in missing


def test_a_static_jwks_source_without_a_uri_is_incomplete(db_session, transport):
    configure_federation(db_session, overrides={"oidc_mobile_jwks_uri": ""})
    with pytest.raises(OidcFederationConfigError) as exc:
        _start(db_session)
    assert "oidc_mobile_jwks_uri" in exc.value.details["missing_settings"]


def test_identifiers_do_not_inherit_from_the_platform_scope(db_session):
    """A platform row must not answer "which issuer" for the operator tenant.

    Without `inherits=False` this resolves to the platform value and the
    deployment federates confidently against the wrong identity provider.
    """

    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.services.operator_tenant import provision_operator_tenant
    from app.services.settings_spec import resolve_value

    provision_operator_tenant(db_session)
    db_session.add(
        DomainSetting(
            tenant_id=None,
            scope_kind="platform",
            domain=SettingDomain.auth,
            key="oidc_mobile_issuer",
            value_text="https://someone-elses-idp.invalid",
            is_active=True,
        )
    )
    db_session.commit()

    assert resolve_value(db_session, SettingDomain.auth, "oidc_mobile_issuer") is None

    # Sensitivity: an INHERITING auth setting in the same table does fall back,
    # so the assertion above is testing `inherits`, not an empty table.
    db_session.add(
        DomainSetting(
            tenant_id=None,
            scope_kind="platform",
            domain=SettingDomain.auth,
            key="oidc_mobile_jwks_source",
            value_text="static_uri",
            is_active=True,
        )
    )
    db_session.commit()
    assert (
        resolve_value(db_session, SettingDomain.auth, "oidc_mobile_jwks_source")
        == "static_uri"
    )


def test_the_configuration_is_read_only_from_the_deployment(db_session, transport):
    configure_federation(db_session)
    config = require_federation_config(db_session)
    assert config.issuer == ISSUER
    assert config.client_id == CLIENT_ID
    assert config.redirect_uri == REDIRECT_URI
    assert config.audience == AUDIENCE
