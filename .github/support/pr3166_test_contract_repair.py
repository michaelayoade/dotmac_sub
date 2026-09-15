"""Keep lifecycle refusal coverage at the actual typed input boundary."""
from pathlib import Path

path = Path('tests/test_subscriber_metadata_key_closure.py')
source = path.read_text()
assert source.count('from app.models.subscriber import Subscriber, SubscriberStatus') == 1
source = source.replace('from app.models.subscriber import Subscriber, SubscriberStatus', 'from app.models.subscriber import Subscriber')
start = source.index('@pytest.mark.parametrize("guard", ["lifecycle", "billing_approval"])')
end = source.index('\n\n@pytest.mark.parametrize(\n    "metadata",', start)
source = source[:start] + '''def test_rejected_billing_update_does_not_dirty_notification_preferences(
    db_session: Session, subscriber: Subscriber
) -> None:
    before = {"billing_notifications": True, "sms_updates": False}
    subscriber.metadata_ = dict(before)
    db_session.commit()
    payload = SubscriberUpdate(
        notification_preferences=_notification_preferences(),
        billing_enabled=not subscriber.billing_enabled,
    )

    with pytest.raises(HTTPException) as caught:
        subscriber_service.Subscribers.update(
            db_session, subscriber_id=str(subscriber.id), payload=payload
        )

    assert caught.value.status_code == 409
    # A rollback would mask an early ORM mutation. Check before rolling back
    # and prove a later flush cannot persist a rejected preference change.
    assert subscriber.metadata_ == before
    assert not db_session.is_modified(subscriber, include_collections=True)
    db_session.flush()
    db_session.refresh(subscriber)
    assert subscriber.metadata_ == before


@pytest.mark.parametrize(("field", "value"), [("status", "blocked"), ("is_active", False)])
def test_lifecycle_fields_are_rejected_before_notification_updates(
    db_session: Session, subscriber: Subscriber, field: str, value: str | bool
) -> None:
    before = {"billing_notifications": True, "sms_updates": False}
    subscriber.metadata_ = dict(before)
    db_session.commit()

    # SubscriberUpdate deliberately excludes lifecycle fields. Exercise the
    # production schema rather than constructing an impossible service input.
    with pytest.raises(ValidationError) as caught:
        SubscriberUpdate.model_validate(
            {
                field: value,
                "notification_preferences": _notification_preferences().model_dump(),
            }
        )

    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        ((field,), "extra_forbidden")
    ]
    assert subscriber.metadata_ == before
    assert not db_session.is_modified(subscriber, include_collections=True)
    db_session.flush()
    db_session.refresh(subscriber)
    assert subscriber.metadata_ == before
''' + source[end:]
path.write_text(source)
