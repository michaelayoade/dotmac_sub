"""Migrated PostgreSQL visibility gate; collected only in local development."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.subscriber import (
    Reseller,
    ResellerUser,
    Subscriber,
    SubscriberStatus,
    UserType,
)
from app.services.reseller_session_standing import (
    ResellerSessionStandingQuery,
    resolve_reseller_session_standing,
)


@pytest.mark.parametrize("principal_type", ["subscriber", "reseller_user"])
def test_fresh_connection_revocation_overrides_warm_identity_map(
    cloned_database, principal_type
):
    engine = create_engine(cloned_database("heads"))
    try:
        assert engine.dialect.name == "postgresql"
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as setup:
            reseller = Reseller(name=f"Standing {uuid4()}", is_active=True)
            setup.add(reseller)
            setup.flush()
            subscriber = Subscriber(
                first_name="Standing",
                last_name="Visibility",
                email=f"{uuid4()}@example.test",
                reseller_id=reseller.id,
                is_active=True,
            )
            setup.add(subscriber)
            setup.flush()
            user = ResellerUser(
                reseller_id=reseller.id,
                is_active=True,
                subscriber_id=subscriber.id if principal_type == "subscriber" else None,
            )
            setup.add(user)
            setup.flush()
            row = AuthSession(
                subscriber_id=subscriber.id if principal_type == "subscriber" else None,
                reseller_user_id=user.id if principal_type == "reseller_user" else None,
                status=SessionStatus.active,
                token_hash=str(uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            setup.add(row)
            setup.commit()
            query = ResellerSessionStandingQuery(
                row.id,
                principal_type,
                subscriber.id if principal_type == "subscriber" else user.id,
                reseller.id,
                row.subscriber_id,
                row.reseller_user_id,
            )
        with sessions() as reader:
            warm = reader.get(AuthSession, query.auth_session_id)
            assert resolve_reseller_session_standing(reader, query=query)
            with sessions() as revoker:
                revoker.execute(
                    update(AuthSession)
                    .where(AuthSession.id == query.auth_session_id)
                    .values(status=SessionStatus.revoked, revoked_at=datetime.now(UTC))
                )
                revoker.commit()
            assert warm.status == SessionStatus.active
            # PostgreSQL READ COMMITTED observes the other connection's commit,
            # despite the reader's already-populated ORM identity map.
            assert resolve_reseller_session_standing(reader, query=query) is None
            assert (
                warm.status == SessionStatus.active
            )  # Scalar reads preserve caller ORM state.
    finally:
        engine.dispose()


def test_first_class_standing_accepts_retired_historical_subscriber(
    cloned_database,
):
    engine = create_engine(cloned_database("heads"))
    try:
        assert engine.dialect.name == "postgresql"
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as setup:
            reseller = Reseller(name=f"Historical standing {uuid4()}", is_active=True)
            setup.add(reseller)
            setup.flush()
            subscriber = Subscriber(
                first_name="Retired",
                last_name="Reseller",
                email=f"{uuid4()}@example.test",
                reseller_id=reseller.id,
                user_type=UserType.reseller,
                status=SubscriberStatus.canceled,
                is_active=False,
            )
            setup.add(subscriber)
            setup.flush()
            user = ResellerUser(
                reseller_id=reseller.id,
                subscriber_id=subscriber.id,
                is_active=True,
            )
            setup.add(user)
            setup.flush()
            row = AuthSession(
                reseller_user_id=user.id,
                status=SessionStatus.active,
                token_hash=str(uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            setup.add(row)
            setup.commit()
            query = ResellerSessionStandingQuery(
                auth_session_id=row.id,
                principal_type="reseller_user",
                principal_id=user.id,
                reseller_id=reseller.id,
                subscriber_id=None,
                reseller_user_id=user.id,
            )
        with sessions() as reader:
            assert resolve_reseller_session_standing(reader, query=query)
    finally:
        engine.dispose()
