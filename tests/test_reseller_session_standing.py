"""SQLite fast-lane evidence; no deployed-schema/concurrency acceptance claim."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.services import reseller_portal as portal
from app.services.reseller_session_standing import (
    ResellerSessionStandingQuery,
    resolve_reseller_session_standing,
)


@pytest.fixture(params=["subscriber", "reseller_user"])
def bound_session(request, db_session, subscriber, monkeypatch):
    reseller = Reseller(name="Standing reseller")
    db_session.add(reseller)
    db_session.flush()
    user = ResellerUser(
        reseller_id=reseller.id,
        subscriber_id=subscriber.id if request.param == "subscriber" else None,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    principal_id = subscriber.id if request.param == "subscriber" else user.id
    row = AuthSession(
        subscriber_id=subscriber.id if request.param == "subscriber" else None,
        reseller_user_id=user.id if request.param == "reseller_user" else None,
        status=SessionStatus.active,
        token_hash=str(uuid4()),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(row)
    db_session.commit()
    query = ResellerSessionStandingQuery(
        row.id,
        request.param,
        principal_id,
        reseller.id,
        subscriber.id if request.param == "subscriber" else None,
        user.id if request.param == "reseller_user" else None,
    )
    payload = {
        "auth_session_id": str(row.id),
        "principal_type": request.param,
        "principal_id": str(principal_id),
        "reseller_id": str(reseller.id),
        "subscriber_id": str(query.subscriber_id) if query.subscriber_id else None,
        "reseller_user_id": str(query.reseller_user_id)
        if query.reseller_user_id
        else None,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "remember": False,
        "is_impersonation": False,
    }
    monkeypatch.setattr(portal, "_get_session", lambda _token: payload)
    monkeypatch.setattr(portal, "invalidate_session", Mock())
    monkeypatch.setattr(portal, "store_session", Mock())
    return query, row, user, payload


def test_ordinary_context_requires_current_db_standing(db_session, bound_session):
    query, row, _, _ = bound_session
    assert resolve_reseller_session_standing(db_session, query=query)
    assert portal.get_context(db_session, "cookie")["principal_id"] == str(
        query.principal_id
    )
    # The same statement used by credential reset/revoke invalidates standing,
    # even though the object loaded above remains stale in the identity map.
    db_session.execute(
        update(AuthSession)
        .where(AuthSession.id == row.id)
        .values(status=SessionStatus.revoked, revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    assert row.status == SessionStatus.active
    assert portal.get_context(db_session, "cookie") is None


@pytest.mark.parametrize(
    "change",
    [
        {"auth_session_id": uuid4()},
        {"principal_id": uuid4()},
        {"reseller_id": uuid4()},
        {"principal_type": "staff"},
        {"subscriber_id": uuid4(), "reseller_user_id": uuid4()},
    ],
)
def test_exact_typed_binding(db_session, bound_session, change):
    query, _, _, _ = bound_session
    assert (
        resolve_reseller_session_standing(db_session, query=replace(query, **change))
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", SessionStatus.revoked),
        ("status", SessionStatus.expired),
        ("revoked_at", datetime.now(UTC)),
        ("expires_at", datetime.now(UTC) - timedelta(seconds=1)),
        ("system_user_id", uuid4()),
        ("party_id", uuid4()),
    ],
)
def test_stale_auth_identity_map_cannot_grant(db_session, bound_session, field, value):
    query, row, _, _ = bound_session
    # Invalid exclusive-FK states cannot be flushed in SQLite; mutate the
    # refreshed row at the read seam to prove defensive refusal independently.
    if field in ("system_user_id", "party_id"):
        facts = SimpleNamespace(
            subscriber_id=row.subscriber_id,
            reseller_user_id=row.reseller_user_id,
            system_user_id=None,
            party_id=None,
            status=row.status,
            revoked_at=None,
            expires_at=row.expires_at,
        )
        setattr(facts, field, value)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                db_session,
                "execute",
                Mock(return_value=Mock(one_or_none=lambda: facts)),
            )
            assert resolve_reseller_session_standing(db_session, query=query) is None
    else:
        db_session.execute(
            update(AuthSession)
            .where(AuthSession.id == row.id)
            .values(**{field: value})
            .execution_options(synchronize_session=False)
        )
        assert resolve_reseller_session_standing(db_session, query=query) is None


@pytest.mark.parametrize("field", ["is_active", "reseller_id"])
def test_stale_reseller_membership_cannot_grant(db_session, bound_session, field):
    query, _, user, _ = bound_session
    value = False
    if field == "reseller_id":
        other = Reseller(name="Other reseller")
        db_session.add(other)
        db_session.flush()
        value = other.id
    db_session.execute(
        update(ResellerUser)
        .where(ResellerUser.id == user.id)
        .values(**{field: value})
        .execution_options(synchronize_session=False)
    )
    assert resolve_reseller_session_standing(db_session, query=query) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("auth_session_id", None),
        ("auth_session_id", "broken"),
        ("principal_id", None),
        ("principal_type", None),
        ("principal_type", "staff"),
        ("reseller_id", "broken"),
        ("subscriber_id", "broken"),
        ("reseller_user_id", "broken"),
        ("is_impersonation", "true"),
        ("is_impersonation", 1),
    ],
)
def test_malformed_unbound_ordinary_cookies_deny(
    db_session, bound_session, field, value
):
    _, _, _, payload = bound_session
    payload[field] = value
    if field == "is_impersonation":
        payload["auth_session_id"] = None
    assert portal.get_context(db_session, "cookie") is None
    assert portal.refresh_session("cookie", db_session) is None
    portal.store_session.assert_not_called()


def test_refresh_database_deadline_and_no_db_denial(
    db_session, bound_session, monkeypatch
):
    _, row, _, payload = bound_session
    monkeypatch.setattr(portal, "_session_ttl_seconds", lambda *_: 86400)
    monkeypatch.setattr(portal, "_absolute_ttl_seconds", lambda *_: 86400)
    refreshed = portal.refresh_session("cookie", db_session)
    assert datetime.fromisoformat(refreshed["expires_at"]) == row.expires_at.replace(
        tzinfo=UTC
    )
    assert portal.store_session.call_args.args[3] <= 600
    portal.store_session.reset_mock()
    assert portal.refresh_session("cookie") is None
    portal.store_session.assert_not_called()


@pytest.mark.parametrize("absolute", [False, True])
def test_refresh_caps_sliding_and_absolute(
    db_session, bound_session, monkeypatch, absolute
):
    _, _, _, payload = bound_session
    now = datetime.now(UTC)
    payload["created_at"] = (now - timedelta(seconds=90)).isoformat()
    monkeypatch.setattr(portal, "_now", lambda: now)
    monkeypatch.setattr(
        portal, "_session_ttl_seconds", lambda *_: 1000 if absolute else 5
    )
    monkeypatch.setattr(portal, "_absolute_ttl_seconds", lambda *_: 100)
    refreshed = portal.refresh_session("cookie", db_session)
    assert datetime.fromisoformat(refreshed["expires_at"]) == now + timedelta(
        seconds=10 if absolute else 5
    )


def test_revoked_refresh_never_writes_redis(db_session, bound_session):
    _, row, _, _ = bound_session
    row.status = SessionStatus.revoked
    db_session.flush()
    assert portal.refresh_session("cookie", db_session) is None
    portal.store_session.assert_not_called()


def test_reads_preserve_flushed_and_pending_caller_writes(db_session, bound_session):
    query, row, user, _ = bound_session
    kept = Reseller(name="Flushed unrelated caller write")
    pending = Reseller(name="Pending unrelated caller write")
    db_session.add(kept)
    db_session.flush()
    reseller = db_session.get(Reseller, query.reseller_id)
    subscriber = (
        db_session.get(Subscriber, user.subscriber_id) if user.subscriber_id else None
    )
    db_session.add(pending)
    reseller.name = "Pending existing reseller name"
    user.full_name = "Pending existing reseller user name"
    row.user_agent = "Pending existing session metadata"
    if subscriber is not None:
        subscriber.first_name = "Pending existing subscriber name"
    flushes = []

    def before_flush(*_args):
        flushes.append(True)

    event.listen(db_session, "before_flush", before_flush)
    try:
        assert resolve_reseller_session_standing(db_session, query=query)
        assert portal.get_context(db_session, "cookie")
        assert portal.refresh_session("cookie", db_session)
        assert not flushes
        assert pending in db_session.new
        assert reseller.name == "Pending existing reseller name"
        assert user.full_name == "Pending existing reseller user name"
        assert row.user_agent == "Pending existing session metadata"
        if subscriber is not None:
            assert subscriber.first_name == "Pending existing subscriber name"
        assert db_session.get(Reseller, kept.id) is kept
        assert db_session.in_transaction()
    finally:
        event.remove(db_session, "before_flush", before_flush)


@pytest.mark.parametrize("flag", ["true", 1])
def test_valid_ordinary_binding_cannot_claim_impersonation(
    db_session, bound_session, flag
):
    _, _, _, payload = bound_session
    payload["is_impersonation"] = flag
    context = portal.get_context(db_session, "cookie")
    assert context["is_impersonation"] is False


def test_operator_membership_is_independent_of_customer_reseller(
    db_session, bound_session, subscriber
):
    query, _, _, _ = bound_session
    other = Reseller(name="Customer owner distinct from operator membership")
    db_session.add(other)
    db_session.flush()
    subscriber.reseller_id = other.id
    db_session.flush()
    assert resolve_reseller_session_standing(db_session, query=query)
    context = portal.get_context(db_session, "cookie")
    assert context["reseller"].id == query.reseller_id
    assert portal.refresh_session("cookie", db_session)


def test_missing_dedicated_subscriber_membership_is_denied(db_session, bound_session):
    query, _, user, _ = bound_session
    if query.principal_type != "subscriber":
        return
    db_session.delete(user)
    db_session.flush()
    assert resolve_reseller_session_standing(db_session, query=query) is None
    assert portal.get_context(db_session, "cookie") is None


def test_database_outage_never_uses_cache(db_session, bound_session, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OperationalError("unavailable", None, None)

    monkeypatch.setattr(db_session, "execute", unavailable)
    with pytest.raises(HTTPException) as caught:
        portal.get_context(db_session, "cookie")
    assert caught.value.status_code == 503
    with pytest.raises(OperationalError):
        portal.refresh_session("cookie", db_session)
    portal.store_session.assert_not_called()


def test_explicit_legacy_impersonation_is_excluded(db_session, bound_session):
    _, _, _, payload = bound_session
    payload["is_impersonation"] = True
    payload["auth_session_id"] = None
    assert portal.get_context(db_session, "cookie")
    assert portal.refresh_session("cookie")


@pytest.mark.parametrize(
    "created", [None, "broken", "2026-10-11T00:00:00", "2000-01-01T00:00:00+00:00"]
)
def test_invalid_or_exhausted_refresh_clock_never_writes(
    db_session, bound_session, created
):
    _, _, _, payload = bound_session
    payload["created_at"] = created
    assert portal.refresh_session("cookie", db_session) is None
    portal.store_session.assert_not_called()


def test_first_class_subscriber_link_must_still_be_active(
    db_session, bound_session, subscriber
):
    query, _, user, _ = bound_session
    if query.principal_type != "reseller_user":
        return
    user.subscriber_id = subscriber.id
    subscriber.is_active = False
    db_session.flush()
    assert resolve_reseller_session_standing(db_session, query=query) is None


def test_inactive_reseller_is_freshly_denied(db_session, bound_session):
    query, _, _, _ = bound_session
    reseller = db_session.get(Reseller, query.reseller_id)
    db_session.execute(
        update(Reseller)
        .where(Reseller.id == reseller.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert reseller.is_active
    assert portal.get_context(db_session, "cookie") is None
    assert portal.refresh_session("cookie", db_session) is None
    portal.store_session.assert_not_called()


@pytest.mark.parametrize("timestamp", [None, "broken", "2026-10-11T00:00:00"])
def test_malformed_projection_expiry_is_denied(monkeypatch, timestamp):
    monkeypatch.setattr(portal, "load_session", lambda *_: {"expires_at": timestamp})
    monkeypatch.setattr(portal, "invalidate_session", Mock())
    assert portal._get_session("cookie") is None
    portal.invalidate_session.assert_called_once_with("cookie")
