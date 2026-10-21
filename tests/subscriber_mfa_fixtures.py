"""Typed persisted MFA fixtures for tests of authentication, not enrollment.

These fixtures deliberately seed state without an enrollment session or its
audit events, so login/reset session-count assertions retain their meaning.
Enrollment behavior is tested through the registered command owner instead.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pyotp
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, MFAMethodType
from app.services import auth_flow


@dataclass(frozen=True, slots=True)
class SubscriberMfaFixture:
    method_id: UUID
    secret: str = field(repr=False)


def seed_enabled_subscriber_mfa(
    db: Session, *, subscriber_id: UUID, label: str = "device"
) -> SubscriberMfaFixture:
    secret = pyotp.random_base32()
    method = MFAMethod(
        subscriber_id=subscriber_id,
        method_type=MFAMethodType.totp,
        label=label,
        secret=auth_flow._encrypt_secret(db, secret),
        is_primary=True,
        enabled=True,
        is_active=True,
        verified_at=datetime.now(UTC),
    )
    db.add(method)
    db.commit()
    return SubscriberMfaFixture(method.id, secret)
