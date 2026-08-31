"""Shared fixtures for the field-mobile OIDC federation tests.

Deliberately builds REAL rows and REAL RS256 signatures. A mocked verifier
proves the caller said the right words; only a real signature over a real key
proves which key actually verified it, and only a real ceremony row proves the
single-use column is what refuses the replay.
"""

from __future__ import annotations

import json
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from sqlalchemy.orm import Session

from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.domain_settings import DomainSetting, SettingDomain
from app.services.credential_party_binding import (
    AUTHENTICATION_BINDING_INSTALL_SCOPE,
    AuthenticationBindingInstallation,
    AuthenticationBindingInstalled,
    CredentialPartyBinding,
    CredentialPrincipalKind,
    bind_credential_party,
    install_authentication_binding,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user

CREDENTIAL_BINDING_SCOPE = "party:credential_authentication_projection"

ISSUER = "https://idp.test.invalid/realms/field"
CLIENT_ID = "io.dotmac.field"
REDIRECT_URI = "https://links.test.invalid/oidc/field/callback"
AUDIENCE = CLIENT_ID
BINDING_KEY = "oidc.field.primary"
DEPLOYMENT_ID = "sub-test-deployment"
JWKS_URI = "https://idp.test.invalid/realms/field/protocol/openid-connect/certs"

_SETTINGS = {
    "oidc_mobile_issuer": ISSUER,
    "oidc_mobile_client_id": CLIENT_ID,
    "oidc_mobile_redirect_uri": REDIRECT_URI,
    "oidc_mobile_audience": AUDIENCE,
    "oidc_mobile_binding_key": BINDING_KEY,
    "oidc_mobile_deployment_id": DEPLOYMENT_ID,
    "oidc_mobile_jwks_source": "static_uri",
    "oidc_mobile_jwks_uri": JWKS_URI,
}


class RecordingTransport:
    """A `dotmac_auth_oidc` transport that counts every call it is asked to make.

    Counting is the point: the bound on refresh is a claim about how many
    requests reach the identity provider, and only a counter can falsify it.
    Sub no longer implements a transport in production — `HttpxTransport`
    inside the package is the shipped one — so this exists purely to make that
    claim observable from Sub's own exchange.

    It implements the package's `Transport` protocol, `post_form` included. The
    method is unreachable from the native path (a public native client runs its
    own code exchange and Sub never sees the authorization code), and it raises
    rather than returning a plausible document so a future change that started
    reaching the token endpoint through here would fail loudly instead of
    quietly succeeding.
    """

    def __init__(self, keys: list[dict[str, Any]] | None = None) -> None:
        self.keys = keys if keys is not None else []
        self.calls: list[str] = []
        self.fail = False

    def get_json(self, url: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("identity provider unreachable")
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": JWKS_URI,
            }
        return {"keys": list(self.keys)}

    def post_form(
        self,
        url: str,
        *,
        data: dict[str, str],
        auth: tuple[str, str] | None,
        timeout: float,
    ) -> dict[str, Any]:
        raise AssertionError(
            "the native path never reaches a token endpoint: the device runs "
            "its own PKCE exchange and Sub never receives the authorization code"
        )


@dataclass
class SigningKey:
    """One RSA key pair plus its public JWK, ready to sign assertions."""

    kid: str
    private_pem: str = field(repr=False)
    public_jwk: dict[str, Any] = field(repr=False)

    @classmethod
    def generate(cls, kid: str = "test-key-1") -> SigningKey:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        raw = jwk.construct(public_pem, "RS256").to_dict()
        public_jwk = {
            name: (value.decode() if isinstance(value, bytes) else value)
            for name, value in raw.items()
        }
        public_jwk["kid"] = kid
        public_jwk["use"] = "sig"
        return cls(kid=kid, private_pem=private_pem, public_jwk=public_jwk)

    def sign(
        self,
        claims: dict[str, Any],
        *,
        kid: str | None = None,
        algorithm: str = "RS256",
    ) -> str:
        return jwt.encode(
            claims,
            self.private_pem,
            algorithm=algorithm,
            headers={"kid": kid or self.kid},
        )


def assertion_claims(
    nonce: str,
    *,
    subject: str = "keycloak-subject-1",
    issuer: str = ISSUER,
    audience: Any = AUDIENCE,
    azp: str | None = None,
    issued_at: int | None = None,
    expires_at: int | None = None,
    not_before: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A valid assertion by default; every field overridable for a negative."""

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "nonce": nonce,
        "iat": issued_at if issued_at is not None else now,
        "exp": expires_at if expires_at is not None else now + 300,
    }
    if not_before is not None:
        claims["nbf"] = not_before
    if azp is not None:
        claims["azp"] = azp
    # Deliberately present in the default: the whole point is that Sub reads
    # none of it. A test that only ever sends clean tokens cannot show that.
    claims["realm_access"] = {"roles": ["admin", "superuser"]}
    claims["resource_access"] = {"sub": {"roles": ["billing:write"]}}
    claims["scope"] = "openid admin"
    claims["groups"] = ["/field/supervisors"]
    if extra:
        claims.update(extra)
    return claims


def sign_without_kid(key: SigningKey, claims: dict[str, Any]) -> str:
    """A well-formed RS256 assertion that names no signing key.

    Its own helper because `SigningKey.sign` always writes a `kid`, and a token
    without one is the shape that must never buy an outbound request: an
    unaddressable key can only be found by trying every published key until one
    verifies, which is not validation.
    """

    return jwt.encode(claims, key.private_pem, algorithm="RS256")


def unsigned_assertion(claims: dict[str, Any]) -> str:
    """Build the hostile ``alg=none`` wire shape without asking JOSE to sign it."""

    def encoded(value: dict[str, Any]) -> str:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return urlsafe_b64encode(payload).rstrip(b"=").decode()

    return f"{encoded({'alg': 'none', 'typ': 'JWT'})}.{encoded(claims)}."


def _set(db: Session, domain: SettingDomain, key: str, value: str) -> None:
    row = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain, DomainSetting.key == key)
        .first()
    )
    if row is None:
        row = DomainSetting(
            tenant_id=OPERATOR_TENANT_ID,
            scope_kind="tenant",
            domain=domain,
            key=key,
            value_text=value,
            is_active=True,
        )
        db.add(row)
    else:
        row.value_text = value
    db.flush()


def configure_federation(
    db: Session,
    *,
    enabled: bool = True,
    overrides: dict[str, str] | None = None,
) -> None:
    """Provision the operator tenant, the settings rows, and the control."""

    provision_operator_tenant(db)
    values = dict(_SETTINGS)
    if overrides:
        values.update(overrides)
    for key, value in values.items():
        _set(db, SettingDomain.auth, key, value)
    _set(
        db,
        SettingDomain.modules,
        "auth_oidc_mobile_federation",
        "true" if enabled else "false",
    )
    db.commit()


def install_oidc_binding(
    db: Session, *, binding_key: str = BINDING_KEY
) -> AuthenticationBindingInstalled:
    """Install the verifier binding an operator installs first.

    It is always ACTIVE. A binding is deactivated later, by
    ``deactivate_binding`` — that is the real operator sequence, and it is the
    only one the canonical projection writer will accept: a technician cannot
    be provisioned against a verifier that is already switched off.
    """

    return install_authentication_binding(
        db,
        AuthenticationBindingInstallation(
            context=CommandContext.system(
                actor="test:oidc-operator",
                scope=AUTHENTICATION_BINDING_INSTALL_SCOPE,
                reason="reviewed OIDC verifier installation fixture",
            ),
            binding_key=binding_key,
            mechanism_code="oidc",
            name="Field mobile OIDC",
        ),
    )


def deactivate_binding(db: Session, binding: AuthenticationBindingInstalled) -> None:
    """Switch an installed verifier off, the way an operator retires one."""

    row = db.get(AuthenticationBinding, binding.binding_id)
    assert row is not None
    row.is_active = False
    db.flush()


def bind_field_technician(
    db: Session,
    binding: AuthenticationBindingInstalled,
    *,
    subject: str = "keycloak-subject-1",
    is_active: bool = True,
    staff_active: bool = True,
):
    """One operator-installed subject binding: credential -> party -> staff.

    Provisioned THROUGH the canonical writer
    (``credential_party_binding.bind_credential_party``), never by handing the
    database an already-projected row. The projection columns are exactly what
    an operator cannot write by hand in production, so a fixture that wrote
    them itself would let every test below pass over a path no operator could
    execute — which is precisely what happened: the writer refused this
    provisioning outright and ~35 green tests said nothing.

    The command commits, and ``execute_owner_command`` requires a
    transaction-free session at entry, so the unprojected rows are committed
    first. That is the operator sequence too: rows exist, then a reviewed
    command projects them.
    """

    provision_operator_tenant(db)
    user, person = add_bound_staff_user(
        db, email=f"tech-{uuid4().hex}@example.test", is_active=staff_active
    )
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.sso,
        username=subject,
        is_active=is_active,
    )
    db.add(credential)
    db.flush()
    # Read every identifier BEFORE the commit. A commit expires these objects,
    # and a lazy refresh afterwards would open the caller transaction the owner
    # command refuses to run inside.
    credential_id = credential.id
    user_id = user.id
    person_id = person.id
    binding_id = binding.binding_id
    db.commit()

    bind_credential_party(
        db,
        CredentialPartyBinding(
            context=CommandContext.system(
                actor="test:oidc-operator",
                scope=CREDENTIAL_BINDING_SCOPE,
                reason="reviewed OIDC subject binding fixture",
            ),
            credential_id=credential_id,
            expected_principal_kind=CredentialPrincipalKind.system_user,
            expected_principal_id=user_id,
            party_id=person_id,
            authentication_binding_id=binding_id,
            tenant_id=OPERATOR_TENANT_ID,
            binding_source="test-fixture",
            binding_reason="Reviewed OIDC subject binding fixture",
        ),
    )
    return user, person, credential
