"""Unit evidence for the credential-transition session projection handler."""

from uuid import uuid4

import pytest

from app.services.events.handlers import credential_session_projection as module
from app.services.events.types import Event, EventType


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module.auth_cache,
        "invalidate_principal_strict",
        lambda kind, ident: seen.append(("cache", f"{kind}:{ident}")) or 0,
    )
    from app.services import customer_portal_session, reseller_portal

    monkeypatch.setattr(
        customer_portal_session,
        "revoke_customer_sessions_for_subscriber",
        lambda ident, db=None, **kw: seen.append(("customer", ident)),
    )
    monkeypatch.setattr(
        reseller_portal,
        "revoke_reseller_sessions_for_subscriber",
        lambda ident, db=None, **kw: seen.append(("reseller_sub", ident)),
    )
    monkeypatch.setattr(
        reseller_portal,
        "revoke_reseller_sessions_for_principal",
        lambda ident, db=None, **kw: seen.append(("reseller_user", str(ident))),
    )
    return seen


def _event(event_type, principal_type="subscriber", invalidate=True, ident=None):
    ident = ident or uuid4()
    payload = {
        "schema_version": 1,
        "principal_type": principal_type,
        "principal_id": str(ident),
    }
    if event_type is EventType.credential_administration_completed:
        payload["invalidate_sessions"] = invalidate
    return Event(event_type=event_type, payload=payload), ident


def test_admin_completed_with_invalidation_revokes_customer_and_reseller(calls):
    event, ident = _event(EventType.credential_administration_completed)
    module.CredentialSessionProjectionHandler().handle(None, event)
    assert ("customer", str(ident)) in calls
    assert ("reseller_sub", str(ident)) in calls


def test_admin_completed_for_reseller_user_revokes_reseller_sessions(calls):
    event, ident = _event(
        EventType.credential_administration_completed, "reseller_user"
    )
    module.CredentialSessionProjectionHandler().handle(None, event)
    assert ("reseller_user", str(ident)) in calls
    assert not [c for c in calls if c[0] in {"customer", "reseller_sub"}]


def test_admin_completed_without_invalidation_only_clears_cache(calls):
    event, _ = _event(EventType.credential_administration_completed, invalidate=False)
    module.CredentialSessionProjectionHandler().handle(None, event)
    assert [c[0] for c in calls] == ["cache"]


def test_enrollment_completed_never_revokes(calls):
    event, _ = _event(EventType.customer_credential_enrollment_completed)
    module.CredentialSessionProjectionHandler().handle(None, event)
    assert [c[0] for c in calls] == ["cache"]


def test_non_matching_event_is_ignored(calls):
    event, _ = _event(EventType.password_recovery_completed)
    event.event_type = EventType.subscriber_created
    module.CredentialSessionProjectionHandler().handle(None, event)
    assert calls == []


def test_unsupported_schema_is_rejected(calls):
    event, _ = _event(EventType.credential_administration_completed)
    event.payload["schema_version"] = 2
    with pytest.raises(ValueError):
        module.CredentialSessionProjectionHandler().handle(None, event)
    assert calls == []
