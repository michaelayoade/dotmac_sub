from app.models.auth import AuthProvider, UserCredential
import pytest

from app.models.subscriber import Reseller, Subscriber, UserType
from app.services import web_admin_resellers as web_admin_resellers_service


def _create_reseller(db_session, name: str = "Reseller A") -> Reseller:
    reseller = Reseller(name=name, code=f"{name[:3].upper()}-001")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


def test_create_reseller_with_user_links_subscriber_without_reseller_users_table(
    db_session, monkeypatch
):
    reseller = _create_reseller(db_session, "Fallback Reseller")
    monkeypatch.setattr(
        web_admin_resellers_service, "_reseller_users_table_available", lambda _db: False
    )
    monkeypatch.setattr(
        web_admin_resellers_service,
        "send_reseller_portal_invite",
        lambda _db, *, email: f"Invitation sent to {email}",
    )

    payload = {
        "first_name": "Fallback",
        "last_name": "User",
        "email": "fallback-user@example.com",
        "username": "fallback-user",
        "password": "Secret123!",
        "role": None,
    }
    web_admin_resellers_service.create_reseller_with_user(
        db_session, reseller=reseller, user_payload=payload
    )

    subscriber = (
        db_session.query(Subscriber)
        .filter(Subscriber.email == payload["email"])
        .one()
    )
    assert subscriber.reseller_id == reseller.id
    assert getattr(subscriber.user_type, "value", subscriber.user_type) == "reseller"

    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .one()
    )
    assert credential.username == payload["username"]


def test_create_and_link_reseller_user_links_subscriber_without_reseller_users_table(
    db_session, monkeypatch
):
    reseller = _create_reseller(db_session, "Fallback Reseller 2")
    monkeypatch.setattr(
        web_admin_resellers_service, "_reseller_users_table_available", lambda _db: False
    )
    monkeypatch.setattr(
        web_admin_resellers_service,
        "send_reseller_portal_invite",
        lambda _db, *, email: f"Invitation sent to {email}",
    )

    web_admin_resellers_service.create_and_link_reseller_user(
        db_session,
        reseller_id=str(reseller.id),
        first_name="Inline",
        last_name="Create",
        email="inline-create@example.com",
        username="inline-create",
        password="Secret123!",
    )

    subscriber = (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "inline-create@example.com")
        .one()
    )
    assert subscriber.reseller_id == reseller.id
    assert getattr(subscriber.user_type, "value", subscriber.user_type) == "reseller"


def test_send_reseller_portal_invite_uses_reseller_login_target(db_session, monkeypatch):
    captured: dict[str, str] = {}

    def _fake_send_user_invite(_db, *, email: str, next_login_path: str | None = None) -> str:
        captured["email"] = email
        captured["next_login_path"] = str(next_login_path)
        return "Invitation sent."

    monkeypatch.setattr(
        web_admin_resellers_service.web_system_user_mutations_service,
        "send_user_invite",
        _fake_send_user_invite,
    )

    note = web_admin_resellers_service.send_reseller_portal_invite(
        db_session,
        email="invitee@example.com",
    )

    assert "sent" in note.lower()
    assert captured["email"] == "invitee@example.com"
    assert captured["next_login_path"] == "/reseller/auth/login?next=/reseller/dashboard"


def test_link_existing_subscriber_to_reseller_rejects_non_customer(db_session, subscriber):
    reseller = _create_reseller(db_session, "Reseller Link Test")
    with pytest.raises(ValueError, match="Only customer subscribers"):
        web_admin_resellers_service.link_existing_subscriber_to_reseller(
            db_session,
            reseller_id=str(reseller.id),
            subscriber_id=str(subscriber.id),
        )


def test_link_existing_subscriber_to_reseller_links_customer(db_session, subscriber):
    reseller = _create_reseller(db_session, "Reseller Link Test 2")
    subscriber.user_type = UserType.customer
    db_session.commit()

    linked = web_admin_resellers_service.link_existing_subscriber_to_reseller(
        db_session,
        reseller_id=str(reseller.id),
        subscriber_id=str(subscriber.id),
    )

    assert linked is True
    db_session.refresh(subscriber)
    assert subscriber.reseller_id == reseller.id
