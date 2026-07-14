"""Enforcement reconciler must not kick sync-gap victims.

A missing radcheck row is an observation of projection drift, not proof the
customer should be offline: the external sync skips credentials it cannot
rebuild a password for, so a paying customer can legitimately have a live
session and no radcheck row. Kicking them drops a session they cannot
re-establish. The reconciler must consult authoritative state (active
subscription + non-blocked subscriber) before kicking, count the gap, and
enqueue the single-writer refresh to repair it instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.catalog import Subscription, SubscriptionStatus


def _fake_radius_db(unserviceable_rows, open_rows):
    fake_cur = MagicMock()
    fake_cur.fetchall.side_effect = [unserviceable_rows, open_rows]
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    return fake_conn


def test_reconciler_spares_entitled_login_and_kicks_the_rest(
    db_session, subscriber, catalog_offer, monkeypatch
):
    # 100077777 is entitled: active subscription, active (non-blocked)
    # subscriber — its missing radcheck row is a sync gap, not a violation.
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="100077777",
        )
    )
    db_session.commit()

    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@radius-test:5432/radius")
    unserviceable = [
        # (username, acctsessionid, nas_ip, framed_ip, radacctid, stale)
        ("100077777", "sess-gap", "10.0.0.1", "100.64.0.5", 1, False),
        ("100066666", "sess-kick", "10.0.0.1", "100.64.0.6", 2, False),
    ]

    with (
        patch("app.db.SessionLocal", return_value=db_session),
        patch("psycopg.connect", return_value=_fake_radius_db(unserviceable, [])),
        patch(
            "app.services.radius_reject.get_reject_networks",
            return_value={},
        ),
        patch(
            "app.services.enforcement._nas_device_by_ip",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.enforcement._send_coa_disconnect",
            return_value=True,
        ) as mock_kick,
        patch("app.tasks.radius_population.refresh_radius_from_subs") as mock_refresh,
    ):
        from app.tasks.radius import run_enforcement_reconciler

        stats = run_enforcement_reconciler()

    # The unentitled login is kicked; the entitled one is spared.
    assert stats["kicked"] == 1
    kicked_usernames = {call.args[2] for call in mock_kick.call_args_list}
    assert kicked_usernames == {"100066666"}
    # The gap is surfaced and repair is enqueued, not enforced.
    assert stats["sync_gap_logins"] == 1
    mock_refresh.delay.assert_called_once()
