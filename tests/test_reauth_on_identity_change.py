"""CoA on network-identity change: reconcile RADIUS, then kick live sessions —
only when an active subscription's served IP / routes / login / profile changed.
"""

from unittest.mock import patch

from app.models.catalog import Subscription, SubscriptionStatus
from app.services import enforcement


def _active_sub(db, subscriber, catalog_offer, ipv4="10.0.0.5"):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login="100012345",
        ipv4_address=ipv4,
    )
    db.add(sub)
    db.commit()
    return sub


def test_signature_changes_with_served_ip(db_session, subscriber, catalog_offer):
    sub = _active_sub(db_session, subscriber, catalog_offer)
    sig1 = enforcement.network_identity_signature(db_session, sub)
    sub.ipv4_address = "10.0.0.9"
    db_session.commit()
    sig2 = enforcement.network_identity_signature(db_session, sub)
    assert sig1 != sig2


def test_reauth_noop_when_unchanged(db_session, subscriber, catalog_offer):
    sub = _active_sub(db_session, subscriber, catalog_offer)
    before = enforcement.network_identity_signature(db_session, sub)
    with (
        patch("app.services.radius.reconcile_subscription_connectivity") as rc,
        patch.object(enforcement, "disconnect_subscription_sessions") as dis,
    ):
        res = enforcement.reauth_subscription_on_identity_change(
            db_session, str(sub.id), before=before, reason="t"
        )
    assert res["changed"] is False
    rc.assert_not_called()
    dis.assert_not_called()


def test_reauth_reconciles_then_kicks_on_change(db_session, subscriber, catalog_offer):
    sub = _active_sub(db_session, subscriber, catalog_offer)
    before = enforcement.network_identity_signature(db_session, sub)
    sub.ipv4_address = "10.0.0.9"
    db_session.commit()

    order: list[str] = []
    with (
        patch(
            "app.services.radius.reconcile_subscription_connectivity",
            side_effect=lambda *a, **k: order.append("reconcile"),
        ),
        patch.object(
            enforcement,
            "disconnect_subscription_sessions",
            side_effect=lambda *a, **k: (order.append("kick"), 1)[1],
        ),
    ):
        res = enforcement.reauth_subscription_on_identity_change(
            db_session, str(sub.id), before=before, reason="t"
        )
    assert res == {"changed": True, "disconnected": 1}
    assert order == ["reconcile", "kick"]  # RADIUS updated BEFORE the kick


def test_reauth_noop_when_not_active(db_session, subscriber, catalog_offer):
    sub = _active_sub(db_session, subscriber, catalog_offer)
    before = enforcement.network_identity_signature(db_session, sub)
    sub.status = SubscriptionStatus.suspended
    sub.ipv4_address = "10.0.0.9"
    db_session.commit()
    with (
        patch("app.services.radius.reconcile_subscription_connectivity") as rc,
        patch.object(enforcement, "disconnect_subscription_sessions") as dis,
    ):
        res = enforcement.reauth_subscription_on_identity_change(
            db_session, str(sub.id), before=before, reason="t"
        )
    assert res["changed"] is False
    rc.assert_not_called()
    dis.assert_not_called()
