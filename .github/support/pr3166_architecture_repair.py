"""Apply the reviewed PR 3166 repair in an isolated checkout, not in main."""

from pathlib import Path
import textwrap

p = Path("app/services/subscriber.py")
s = p.read_text()
start = s.index('        notification_preferences = data.pop("notification_preferences", None)')
end = s.index('        lifecycle_fields = {"status", "is_active"} & data.keys()', start)
assert s[start:end].count("subscriber.metadata_ = metadata") == 1
s = s[:start] + '''        data.pop("notification_preferences", None)
        notification_preferences = payload.notification_preferences
        if notification_preferences is not None:
            # An explicit metadata replacement was checked above. Otherwise keep
            # historical data opaque and change only the typed preference keys.
            metadata = dict(data.get("metadata_", subscriber.metadata_) or {})
            metadata["billing_notifications"] = (
                notification_preferences.billing_notifications
            )
            metadata["sms_updates"] = notification_preferences.sms_updates
            metadata["push_notifications"] = notification_preferences.push_notifications
            metadata["service_notifications"] = (
                notification_preferences.service_notifications
            )
            metadata["account_notifications"] = (
                notification_preferences.account_notifications
            )
            metadata["usage_notifications"] = (
                notification_preferences.usage_notifications
            )
            metadata["general_notifications"] = (
                notification_preferences.general_notifications
            )
            # Reuse the owner's existing application point below, after every
            # guard. Do not dirty the row while validating a rejected command.
            data["metadata_"] = metadata
''' + s[end:]
p.write_text(s)

# Preserve the exact historical fixture and assertions, on the existing
# compatibility surface rather than expanding the retired-vocabulary surface.
p = Path("tests/test_customer_portal_notifications.py")
s = p.read_text()
start = s.index("    def test_profile_save_preserves_undeclared_historical_metadata(")
end = s.index("    def test_update_customer_profile_persists_preferences_and_emits_subscriber_updated(", start)
test = textwrap.dedent(s[start:end]).rstrip()
test = test.replace("    self, db_session, subscriber\n", "    db_session: Session, subscriber: Subscriber\n")
test = test.replace(
    ") -> None:\n    from app.services.web_customer_actions",
    ') -> None:\n    """Frozen import provenance survives a live profile/preferences save."""\n    from app.services.web_customer_actions',
)
assert "self," not in test
p.write_text(s[:start] + s[end:])
p = Path("tests/test_crm_portal_services.py")
s = p.read_text()
assert "test_profile_save_preserves_undeclared_historical_metadata" not in s
s = s.replace("from fastapi import Request\n", "from fastapi import Request\nfrom sqlalchemy.orm import Session\n")
s = s.replace("from app.models.support import", "from app.models.subscriber import Subscriber\nfrom app.models.support import", 1)
p.write_text(s.rstrip() + "\n\n\n" + test + "\n")

p = Path("tests/test_subscriber_metadata_key_closure.py")
s = p.read_text()
s = s.replace(
    "import pytest\n\nfrom app.schemas.subscriber import SubscriberUpdate\n",
    "import pytest\nfrom fastapi import HTTPException\nfrom pydantic import ValidationError\nfrom sqlalchemy.orm import Session\n\nfrom app.models.subscriber import Subscriber, SubscriberStatus\nfrom app.schemas.subscriber import (\n    SubscriberNotificationPreferencesUpdate,\n    SubscriberUpdate,\n)\n",
)
assert "from pydantic import ValidationError" in s
s += '''

# Typed preference patches use the same guarded update path as profile fields.


def _notification_preferences() -> SubscriberNotificationPreferencesUpdate:
    return SubscriberNotificationPreferencesUpdate(
        billing_notifications=False,
        sms_updates=True,
        push_notifications=False,
        service_notifications=True,
        account_notifications=False,
        usage_notifications=True,
        general_notifications=False,
    )


@pytest.mark.parametrize("guard", ["lifecycle", "billing_approval"])
def test_rejected_update_does_not_dirty_notification_preferences(
    db_session: Session, subscriber: Subscriber, guard: str
) -> None:
    before = {"billing_notifications": True, "sms_updates": False}
    subscriber.metadata_ = dict(before)
    db_session.commit()
    preferences = _notification_preferences()
    payload = (
        SubscriberUpdate(
            notification_preferences=preferences,
            status=SubscriberStatus.blocked,
        )
        if guard == "lifecycle"
        else SubscriberUpdate(
            notification_preferences=preferences,
            billing_enabled=not subscriber.billing_enabled,
        )
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


@pytest.mark.parametrize(
    "metadata", [None, {}, {"nin_verified": True, "sms_updates": False}]
)
def test_explicit_metadata_and_preferences_are_applied_together(
    db_session: Session, subscriber: Subscriber, metadata: dict[str, bool] | None
) -> None:
    subscriber.metadata_ = {"portal_read_notification_keys": ["old-notice"]}
    db_session.commit()
    preferences = _notification_preferences()
    updated = subscriber_service.Subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(
            metadata_=metadata, notification_preferences=preferences
        ),
    )
    assert updated.metadata_ == {**(metadata or {}), **preferences.model_dump()}


def test_preference_patch_cannot_bypass_undeclared_metadata_guard(
    db_session: Session, subscriber: Subscriber
) -> None:
    before = dict(subscriber.metadata_ or {})
    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        subscriber_service.Subscribers.update(
            db_session,
            subscriber_id=str(subscriber.id),
            payload=SubscriberUpdate(
                metadata_={"invented_by_a_caller": True},
                notification_preferences=_notification_preferences(),
            ),
        )
    assert caught.value.keys == frozenset({"invented_by_a_caller"})
    assert dict(subscriber.metadata_ or {}) == before
    assert not db_session.is_modified(subscriber, include_collections=True)


def test_preference_patch_keeps_a_closed_field_set() -> None:
    with pytest.raises(ValidationError):
        SubscriberNotificationPreferencesUpdate.model_validate(
            {
                **_notification_preferences().model_dump(),
                "invented_by_a_caller": True,
            }
        )


@pytest.mark.parametrize("explicit_null", [False, True])
def test_absent_or_null_preference_patch_preserves_existing_metadata(
    db_session: Session, subscriber: Subscriber, explicit_null: bool
) -> None:
    before = {
        "frozen_import_provenance": {"source": "historical"},
        "sms_updates": False,
    }
    subscriber.metadata_ = dict(before)
    db_session.commit()
    payload = (
        SubscriberUpdate(first_name="Updated", notification_preferences=None)
        if explicit_null
        else SubscriberUpdate(first_name="Updated")
    )
    updated = subscriber_service.Subscribers.update(
        db_session, subscriber_id=str(subscriber.id), payload=payload
    )
    assert updated.first_name == "Updated"
    assert updated.metadata_ == before
'''
p.write_text(s)

p = Path("docs/SUBSCRIBER_METADATA_OWNERSHIP.md")
s = p.read_text()
marker = "**Two deletion lineages, one lifecycle.**"
assert s.count(marker) == 1
s = s.replace(marker, '''The merge is staged in the existing subscriber update payload, not written to
an ORM row before validation. Lifecycle and billing-approval refusals leave the
row clean even before rollback. An explicitly supplied `metadata_` replacement
still passes the closed-key guard and keeps its replacement semantics; the typed
preference values are applied over that replacement in the same update. An
absent or null preference patch leaves existing metadata unchanged.

The optional `SubscriberUpdate.notification_preferences` API field is additive.
Its seven boolean fields reject extra keys. Regenerate the OpenAPI contract
manifest with `python scripts/update_openapi_contract.py` to record this
intentional shape; no route or existing required field changes. Regression
coverage lives in `test_subscriber_metadata_key_closure.py`
and `test_customer_portal_notifications.py`; the exact frozen import-key fixture
is retained in the existing `test_crm_portal_services.py` compatibility surface.
The cohort writer-site and vocabulary-freeze baselines are unchanged.

''' + marker)
p.write_text(s)
