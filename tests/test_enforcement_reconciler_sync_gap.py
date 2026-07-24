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

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.enforcement import (
    CoADisconnectDisposition,
    CoADisconnectOutcome,
)


def _fake_radius_db(unserviceable_rows, open_rows):
    fake_cur = MagicMock()
    fake_cur.fetchall.side_effect = [
        unserviceable_rows,
        open_rows,
        [],
        [],
        [],
        [],
        [],
        [],
    ]
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

    target = {
        "db_url": "postgresql://u:p@radius-test:5432/radius",
        "radacct_table": "radacct",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "target_fingerprint": "test-radius",
        "use_group": False,
    }
    unserviceable = [
        # (username, acctsessionid, nas_ip, framed_ip, radacctid, stale)
        ("100077777", "sess-gap", "10.0.0.1", "100.64.0.5", 1, False),
        ("100066666", "sess-kick", "10.0.0.1", "100.64.0.6", 2, False),
    ]

    with (
        patch("app.db.SessionLocal", return_value=db_session),
        patch(
            "app.tasks.radius.postgres_session_advisory_lock",
            return_value=nullcontext(True),
        ),
        patch.object(db_session, "rollback"),
        patch(
            "app.services.external_radius_targets.authoritative_accounting_target",
            return_value=target,
        ),
        patch("psycopg.connect", return_value=_fake_radius_db(unserviceable, [])),
        patch(
            "app.services.radius_reject.get_reject_networks",
            return_value={},
        ),
        patch(
            "app.services.radius_population.populate",
            return_value={
                "unbuildable_logins": 0,
                "expected_projection_fingerprints": {"test-radius": {}},
            },
        ),
        patch(
            "app.services.enforcement._nas_device_by_ip",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.enforcement._send_coa_disconnect",
            return_value=CoADisconnectOutcome(CoADisconnectDisposition.disconnected),
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


@pytest.mark.parametrize(
    ("disposition", "expected_closed", "expected_failed", "expected_status"),
    [
        (CoADisconnectDisposition.session_not_found, 1, 0, "ok"),
        (CoADisconnectDisposition.timeout, 0, 1, "degraded"),
        (CoADisconnectDisposition.rejected, 0, 1, "degraded"),
    ],
)
def test_reconciler_closes_accounting_only_on_verified_session_absence(
    db_session,
    disposition,
    expected_closed,
    expected_failed,
    expected_status,
):
    target = {
        "db_url": "postgresql://u:p@radius-test:5432/radius",
        "radacct_table": "radacct",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "target_fingerprint": "test-radius",
        "use_group": False,
    }
    unserviceable = [
        ("orphan-login", "sess-orphan", "10.0.0.1", "100.64.0.6", 42, True)
    ]
    radius_db = _fake_radius_db(unserviceable, [])

    with (
        patch("app.db.SessionLocal", return_value=db_session),
        patch(
            "app.tasks.radius.postgres_session_advisory_lock",
            return_value=nullcontext(True),
        ),
        patch.object(db_session, "rollback"),
        patch(
            "app.services.external_radius_targets.authoritative_accounting_target",
            return_value=target,
        ),
        patch("psycopg.connect", return_value=radius_db),
        patch("app.services.radius_reject.get_reject_networks", return_value={}),
        patch(
            "app.services.radius_population.populate",
            return_value={
                "unbuildable_logins": 0,
                "expected_projection_fingerprints": {"test-radius": {}},
            },
        ),
        patch(
            "app.services.enforcement._nas_device_by_ip",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.enforcement._send_coa_disconnect",
            return_value=CoADisconnectOutcome(disposition),
        ),
        patch("app.services.observability.record_task_run") as record_task_run,
    ):
        from app.tasks.radius import run_enforcement_reconciler

        stats = run_enforcement_reconciler()

    assert stats["ghosts_closed"] == expected_closed
    assert stats["kick_failed"] == expected_failed
    assert radius_db.commit.call_count == expected_closed
    assert record_task_run.call_args.kwargs["status"] == expected_status
