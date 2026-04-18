from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.catalog.subscriptions import apply_offer_radius_profile


def _mock_db_with_credentials(*credentials):
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = (
        list(credentials)
    )
    return db


def test_apply_offer_radius_profile_sets_offer_default_for_inherited_subscription():
    subscription = SimpleNamespace(
        subscriber_id=uuid4(),
        offer_id=uuid4(),
        radius_profile_id=None,
    )
    credential = SimpleNamespace(radius_profile_id=None)
    new_profile_id = uuid4()
    db = _mock_db_with_credentials(credential)

    with patch(
        "app.services.catalog.subscriptions._resolve_offer_radius_profile_id",
        side_effect=[None, new_profile_id],
    ):
        resolved = apply_offer_radius_profile(
            db,
            subscription,
            previous_offer_id=uuid4(),
        )

    assert resolved == new_profile_id
    assert subscription.radius_profile_id == new_profile_id
    assert credential.radius_profile_id == new_profile_id


def test_apply_offer_radius_profile_preserves_manual_override():
    old_default = uuid4()
    new_default = uuid4()
    manual_profile = uuid4()
    subscription = SimpleNamespace(
        subscriber_id=uuid4(),
        offer_id=uuid4(),
        radius_profile_id=manual_profile,
    )
    credential = SimpleNamespace(radius_profile_id=manual_profile)
    db = _mock_db_with_credentials(credential)

    with patch(
        "app.services.catalog.subscriptions._resolve_offer_radius_profile_id",
        side_effect=[old_default, new_default],
    ):
        resolved = apply_offer_radius_profile(
            db,
            subscription,
            previous_offer_id=uuid4(),
        )

    assert resolved == new_default
    assert subscription.radius_profile_id == manual_profile
    assert credential.radius_profile_id == manual_profile


def test_apply_offer_radius_profile_forced_profile_updates_credentials():
    old_default = uuid4()
    manual_profile = uuid4()
    subscription = SimpleNamespace(
        subscriber_id=uuid4(),
        offer_id=uuid4(),
        radius_profile_id=old_default,
    )
    credential = SimpleNamespace(radius_profile_id=old_default)
    db = _mock_db_with_credentials(credential)

    with patch(
        "app.services.catalog.subscriptions._resolve_offer_radius_profile_id",
        return_value=old_default,
    ):
        resolved = apply_offer_radius_profile(
            db,
            subscription,
            previous_offer_id=uuid4(),
            target_profile_id=manual_profile,
            force=True,
        )

    assert resolved == manual_profile
    assert subscription.radius_profile_id == manual_profile
    assert credential.radius_profile_id == manual_profile
