"""PostgreSQL proof for the opaque Integrator-to-provider mapping."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.billing import PaymentProvider, PaymentProviderType

pytestmark = pytest.mark.integration


def test_migration_550_created_one_nullable_uuid_mapping(db_session) -> None:
    column = db_session.execute(
        text(
            "SELECT is_nullable, udt_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'payment_providers' "
            "AND column_name = 'integrator_installation_ref'"
        )
    ).one()
    constraint = db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'payment_providers'::regclass "
            "AND contype = 'u' "
            "AND conname = 'uq_payment_providers_integrator_installation_ref'"
        )
    ).scalar_one_or_none()

    assert tuple(column) == ("YES", "uuid")
    assert constraint == "uq_payment_providers_integrator_installation_ref"


def test_one_integrator_installation_cannot_select_two_providers(db_session) -> None:
    installation_id = uuid4()
    db_session.add_all(
        [
            PaymentProvider(
                name=f"Integrator mapping A {uuid4().hex}",
                provider_type=PaymentProviderType.custom,
                integrator_installation_ref=installation_id,
                is_active=True,
            ),
            PaymentProvider(
                name=f"Integrator mapping B {uuid4().hex}",
                provider_type=PaymentProviderType.custom,
                integrator_installation_ref=installation_id,
                is_active=True,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()
