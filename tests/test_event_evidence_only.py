"""Control-plane evidence is durable without entering event delivery."""

from app.models.event_store import EventStatus, EventStore
from app.services import event_store as event_store_service
from app.services.events import dispatcher as dispatcher_module
from app.services.events.dispatcher import EventDispatcher, emit_event
from app.services.events.types import EventType


def _unexpected_dispatch(*_args, **_kwargs):
    raise AssertionError(
        "record-only evidence must not initialize or dispatch handlers"
    )


def test_record_only_event_is_completed_and_never_dispatchable(db_session, monkeypatch):
    monkeypatch.setattr(dispatcher_module, "get_dispatcher", _unexpected_dispatch)
    monkeypatch.setattr(dispatcher_module, "run_after_commit", _unexpected_dispatch)

    event = emit_event(
        db_session,
        EventType.communication_intent_planned,
        {"schema_version": 1, "intent_id": "test-intent"},
        defer_until_commit=False,
        dispatch_after_commit=True,
        record_only=True,
    )
    record = db_session.query(EventStore).filter_by(event_id=event.event_id).one()
    assert record.status is EventStatus.completed
    assert record.processed_at is not None
    assert record.failed_handlers is None
    assert record.retry_count == 0
    assert event_store_service.list_pending_event_ids(db_session, limit=10) == []
    assert EventDispatcher().dispatch_pending_event(db_session, record.id) is False

    db_session.commit()
    db_session.expire_all()
    assert (
        db_session.query(EventStore).filter_by(event_id=event.event_id).one().status
        is EventStatus.completed
    )
    assert event_store_service.list_pending_event_ids(db_session, limit=10) == []


def test_record_only_dry_adapter_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(dispatcher_module, "get_dispatcher", _unexpected_dispatch)
    monkeypatch.setattr(dispatcher_module, "run_after_commit", _unexpected_dispatch)

    event = emit_event(
        object(),  # type: ignore[arg-type] -- legacy non-SQL dry adapter
        EventType.communication_intent_planned,
        {"schema_version": 1, "intent_id": "dry-run"},
        defer_until_commit=False,
        record_only=True,
    )
    assert event.event_type is EventType.communication_intent_planned


def test_ordinary_deferred_event_remains_pending_and_dispatchable(
    db_session, monkeypatch
):
    dispatcher = EventDispatcher()
    monkeypatch.setattr(dispatcher_module, "get_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(dispatcher_module, "run_after_commit", _unexpected_dispatch)

    event = emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": "ordinary-deferred"},
        defer_until_commit=True,
        dispatch_after_commit=False,
    )
    record = db_session.query(EventStore).filter_by(event_id=event.event_id).one()
    assert record.status is EventStatus.pending
    assert record.processed_at is None
    assert event_store_service.list_pending_event_ids(db_session, limit=10) == [
        record.id
    ]

    assert dispatcher.dispatch_pending_event(db_session, record.id) is True
    assert record.status is EventStatus.completed
    assert record.processed_at is not None
