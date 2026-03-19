from app.models.auth import AuthProvider, UserCredential
from app.services.auth_flow import verify_password
from scripts.seed_admin import seed_admin_user


def test_seed_admin_user_creates_local_credential(db_session, subscriber):
    message = seed_admin_user(
        db_session,
        email=subscriber.email,
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        username="admin",
        password="AdminPass123!",
        force_reset=False,
    )

    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .one()
    )

    assert message == "Admin user created."
    assert credential.username == "admin"
    assert verify_password("AdminPass123!", credential.password_hash) is True
    assert credential.must_change_password is False


def test_seed_admin_user_updates_existing_local_credential(db_session, subscriber):
    existing = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="old-admin",
        password_hash="old-hash",
        must_change_password=False,
        is_active=False,
        failed_login_attempts=5,
    )
    db_session.add(existing)
    db_session.commit()

    message = seed_admin_user(
        db_session,
        email=subscriber.email,
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        username="admin",
        password="NewAdminPass123!",
        force_reset=True,
    )

    db_session.refresh(existing)

    assert message == "Admin user updated."
    assert existing.username == "admin"
    assert verify_password("NewAdminPass123!", existing.password_hash) is True
    assert existing.must_change_password is True
    assert existing.is_active is True
    assert existing.failed_login_attempts == 0
    assert existing.locked_until is None
