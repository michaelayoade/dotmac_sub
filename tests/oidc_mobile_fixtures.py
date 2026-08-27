"""Shared fixtures for the field-mobile OIDC federation tests.

Deliberately builds REAL rows and REAL RS256 signatures. A mocked verifier
proves the caller said the right words; only a real signature over a real key
proves which key actually verified it, and only a real ceremony row proves the
single-use column is what refuses the replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from sqlalchemy.orm import Session

from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.domain_settings import DomainSetting, SettingDomain
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from tests.staff_identity_fixtures import add_bound_staff_user

ISSUER = "https://idp.test.invalid/realms/field"
CLIENT_ID = "dotmac-field-mobile"
REDIRECT_URI = "https://links.test.invalid/oidc/field/callback"
AUDIENCE = "io.dotmac.field"
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
    """A JWKS transport that counts every outbound call it is asked to make.

    Counting is the point: the bound on refresh is a claim about how many
    requests reach the identity provider, and only a counter can falsify it.
    """

    def __init__(self, keys: list[dict[str, Any]] | None = None) -> None:
        self.keys = keys if keys is not None else []
        self.calls: list[str] = []
        self.fail = False

    def get_json(self, url: str, *, timeout_seconds: int) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("identity provider unreachable")
        return {"keys": list(self.keys)}


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
    db: Session, *, binding_key: str = BINDING_KEY, is_active: bool = True
) -> AuthenticationBinding:
    binding = AuthenticationBinding(
        binding_key=binding_key,
        mechanism_code="oidc",
        name="Field mobile OIDC",
        is_active=is_active,
    )
    db.add(binding)
    db.flush()
    return binding


def bind_field_technician(
    db: Session,
    binding: AuthenticationBinding,
    *,
    subject: str = "keycloak-subject-1",
    is_active: bool = True,
    staff_active: bool = True,
):
    """One operator-installed subject binding: credential -> party -> staff."""

    user, person = add_bound_staff_user(
        db, email=f"tech-{uuid4().hex}@example.test", is_active=staff_active
    )
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.sso,
        username=subject,
        is_active=is_active,
        party_id=person.id,
        authentication_binding_id=binding.id,
        tenant_id=OPERATOR_TENANT_ID,
        party_bound_at=user.party_bound_at,
        party_binding_source="test-fixture",
        party_binding_reason="Reviewed OIDC subject binding fixture",
    )
    db.add(credential)
    db.flush()
    return user, person, credential
