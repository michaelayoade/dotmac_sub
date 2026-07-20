from __future__ import annotations

from types import SimpleNamespace

from app.api import billing as billing_api
from app.api import subscribers as subscribers_api
from app.services import auth_dependencies


def test_subscriber_detail_releases_read_transaction(monkeypatch) -> None:
    db = object()
    subscriber = SimpleNamespace(id="sub-1")
    calls: list[tuple[object, object]] = []

    monkeypatch.setattr(
        subscribers_api.subscriber_service.subscribers,
        "get",
        lambda got_db, subscriber_id: subscriber,
    )
    monkeypatch.setattr(
        subscribers_api,
        "finish_read_response",
        lambda got_db, value: calls.append((got_db, value)) or value,
    )

    result = subscribers_api.get_subscriber("sub-1", db=db)

    assert result is subscriber
    assert calls == [(db, subscriber)]


def test_billing_ledger_detail_releases_read_transaction(monkeypatch) -> None:
    db = object()
    ledger_entry = SimpleNamespace(id="entry-1")
    calls: list[tuple[object, object]] = []

    monkeypatch.setattr(
        billing_api.billing_service.ledger_entries,
        "get",
        lambda got_db, entry_id: ledger_entry,
    )
    monkeypatch.setattr(
        billing_api,
        "finish_read_response",
        lambda got_db, value: calls.append((got_db, value)) or value,
    )

    result = billing_api.get_ledger_entry("entry-1", db=db)

    assert result is ledger_entry
    assert calls == [(db, ledger_entry)]


def test_permission_fast_path_releases_auth_transaction(monkeypatch) -> None:
    db = object()
    auth = {"roles": ["admin"], "scopes": []}
    calls: list[object] = []

    monkeypatch.setattr(
        auth_dependencies,
        "finish_read_transaction",
        lambda got_db: calls.append(got_db),
    )

    dependency = auth_dependencies.require_permission("billing:invoice:read")
    result = dependency(auth=auth, db=db)

    assert result is auth
    assert calls == [db]
