"""An undeclared `subscribers.metadata` key is REFUSED, not ignored.

The distinction is the whole point of these tests. Until 2026-08-22 an
unrendered admin form field named `metadata` accepted arbitrary JSON and
`web_customer_actions` wrote it to the column wholesale, so any caller could
invent any key on any subscriber. Closing that could have been done three ways,
and two of them are wrong:

- **Ignore** the unknown key. The caller believes the write succeeded, the
  value is gone, and the failure surfaces later as absent data with nothing
  attached to the moment it was lost.
- **Sanitise** it — strip, rename or coerce. Same silence, plus a value that
  differs from what was sent without anyone being told.
- **Refuse** it, naming the key. This is what the code does, and what these
  tests pin.

The tests therefore assert an exception, and separately assert that the row was
NOT modified. A guard that raises after writing is not a guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.subscriber import SubscriberUpdate
from app.services import subscriber as subscriber_service
from app.services.subscriber_metadata_keys import (
    DECLARED_METADATA_KEYS,
    UndeclaredMetadataKeyError,
    reject_undeclared_keys,
    undeclared_keys,
)

#: Anchored to the repository, not the working directory: these two checks read
#: source files, and a cwd-relative path makes them pass vacuously from the
#: wrong directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------------


def test_a_declared_key_is_accepted() -> None:
    reject_undeclared_keys({"nin_verified": True})


def test_an_undeclared_key_raises_and_names_itself() -> None:
    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        reject_undeclared_keys({"invented_by_a_caller": "anything"})
    assert "invented_by_a_caller" in str(caught.value), (
        "the refusal must name the key; an unnamed refusal sends the caller "
        "looking through a whole payload"
    )
    assert caught.value.keys == frozenset({"invented_by_a_caller"})


def test_every_undeclared_key_is_reported_not_just_the_first() -> None:
    """A caller fixing one key at a time learns of the next one on the retry."""

    with pytest.raises(UndeclaredMetadataKeyError) as caught:
        reject_undeclared_keys({"first_invented": 1, "second_invented": 2})
    assert caught.value.keys == frozenset({"first_invented", "second_invented"})


def test_a_declared_key_beside_an_undeclared_one_does_not_rescue_the_write() -> None:
    """Partial validity is not validity. The whole write is refused."""

    with pytest.raises(UndeclaredMetadataKeyError):
        reject_undeclared_keys({"nin_verified": True, "invented": 1})


def test_the_registry_names_an_owner_for_every_key() -> None:
    ownerless = sorted(
        key for key, owner in DECLARED_METADATA_KEYS.items() if not owner.strip()
    )
    assert not ownerless, (
        "these keys are declared with no owner, which is the state the registry "
        "exists to end:\n  " + "\n  ".join(ownerless)
    )


def test_a_non_dict_value_is_not_treated_as_declared() -> None:
    """`undeclared_keys` must not silently pass a malformed payload."""

    assert undeclared_keys(None) == frozenset()
    assert undeclared_keys("not a dict") == frozenset()


# --------------------------------------------------------------------------
# the owner refuses, and refuses BEFORE writing
# --------------------------------------------------------------------------


def test_the_owner_refuses_an_undeclared_key_on_update(db_session, subscriber):
    before = dict(subscriber.metadata_ or {})

    with pytest.raises(Exception) as caught:
        subscriber_service.Subscribers.update(
            db_session,
            str(subscriber.id),
            SubscriberUpdate(metadata_={"invented_by_a_caller": "anything"}),
        )
    assert "invented_by_a_caller" in str(caught.value)

    db_session.rollback()
    refreshed = db_session.get(type(subscriber), subscriber.id)
    assert dict(refreshed.metadata_ or {}) == before, (
        "the row changed despite the refusal. A guard that raises after writing "
        "is not a guard — the caller sees an error and the data is modified."
    )


def test_the_owner_accepts_a_declared_key_on_update(db_session, subscriber):
    """The refusal must not be indiscriminate, or it proves nothing."""

    subscriber_service.Subscribers.update(
        db_session,
        str(subscriber.id),
        SubscriberUpdate(metadata_={"nin_verified": True}),
    )
    refreshed = db_session.get(type(subscriber), subscriber.id)
    assert (refreshed.metadata_ or {}).get("nin_verified") is True


# --------------------------------------------------------------------------
# the wildcard surface itself is gone
# --------------------------------------------------------------------------


def test_the_admin_customer_forms_accept_no_metadata_field() -> None:
    """The field was never rendered by a template — an unused write surface.

    Removing the plumbing matters as much as the guard: a refusal at the owner
    still leaves an endpoint whose contract advertises arbitrary JSON, and the
    next person to need "somewhere to put something" would find it.
    """

    source = (REPOSITORY_ROOT / "app/web/admin/customers.py").read_text(
        encoding="utf-8"
    )
    assert "metadata: str | None = Form(None)" not in source, (
        "the admin customer form still declares a free-JSON `metadata` field"
    )
    assert "metadata_json" not in source, (
        "the admin customer route still plumbs `metadata_json` to the service"
    )


def test_no_service_still_forwards_a_free_json_metadata_payload() -> None:
    for module in (
        "app/services/web_customer_actions.py",
        "app/services/web_subscriber_actions.py",
    ):
        source = (REPOSITORY_ROOT / module).read_text(encoding="utf-8")
        assert "metadata_json" not in source, (
            f"{module} still forwards a free-JSON metadata payload; the second "
            "caller of this shape was found only after the first was removed"
        )
