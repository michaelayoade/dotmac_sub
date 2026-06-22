"""Tests for set_subscription_access_state — the phase 3 shadow-write
dual-write of subscription.access_state + external RADIUS radusergroup.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.services.radius_access_state import set_subscription_access_state

# Reuse the sqlite fixture pattern from test_radius_services.py.


def _write_radusergroup_sqlite(db_path, rows=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
        )
        for row in rows or []:
            conn.execute(
                "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _read_radusergroup(db_path, username):
    conn = sqlite3.connect(db_path)
    try:
        return sorted(
            conn.execute(
                "SELECT username, groupname, priority FROM radusergroup WHERE username = ?",
                (username,),
            )
        )
    finally:
        conn.close()


def _fake_config(db_path):
    return {
        "db_url": f"sqlite:///{db_path}",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "nas_table": "nas",
    }


def _seed_subscription(
    db_session,
    subscriber,
    catalog_offer,
    *,
    username,
    status=SubscriptionStatus.active,
):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
    )
    db_session.add(sub)
    db_session.flush()
    cred = AccessCredential(
        subscriber_id=subscriber.id,
        username=username,
        is_active=True,
    )
    db_session.add(cred)
    db_session.commit()
    return sub, cred


# Map AccessState → matching SubscriptionStatus so the aggregate agrees
# with the per-sub state we're testing.
_STATUS_FOR_STATE = {
    AccessState.active: SubscriptionStatus.active,
    AccessState.suspended: SubscriptionStatus.suspended,
    AccessState.captive: SubscriptionStatus.suspended,  # + captive flag
    AccessState.terminated: SubscriptionStatus.canceled,
}


class TestSetAccessStateWrites:
    @pytest.mark.parametrize(
        "state,expected_aggregate,expected_group",
        [
            (AccessState.active, "active", "dotmac-active"),
            # Captive redirect is opt-in: a suspended-status sub with NO opt-in
            # aggregates to suspended (hard reject); only an opted-in sub →
            # captive (the captive case sets captive_redirect_enabled below).
            (AccessState.suspended, "suspended", "dotmac-suspended"),
            (AccessState.captive, "captive", "dotmac-captive"),
        ],
    )
    def test_state_inserts_correct_group_row(
        self,
        state,
        expected_aggregate,
        expected_group,
        db_session,
        tmp_path,
        subscriber,
        catalog_offer,
    ):
        # The captive aggregate requires the per-customer opt-in; without it a
        # suspended-status sub hard-rejects (dotmac-suspended).
        if state == AccessState.captive:
            subscriber.captive_redirect_enabled = True
            db_session.commit()
        sub, _ = _seed_subscription(
            db_session,
            subscriber,
            catalog_offer,
            username="set-state-1",
            status=_STATUS_FOR_STATE[state],
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            result = set_subscription_access_state(db_session, str(sub.id), state)

        assert result["external_rows_written"] == 1
        assert result["aggregate_state"] == expected_aggregate
        assert _read_radusergroup(radius_db, "set-state-1") == [
            ("set-state-1", expected_group, 0)
        ]
        # App DB column reflects the per-sub state that was set.
        db_session.refresh(sub)
        assert sub.access_state == state.value

    def test_terminated_deletes_only_no_insert(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        sub, _ = _seed_subscription(
            db_session,
            subscriber,
            catalog_offer,
            username="set-state-2",
            status=SubscriptionStatus.canceled,
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(
            radius_db, rows=[("set-state-2", "dotmac-active", 0)]
        )
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            result = set_subscription_access_state(
                db_session, str(sub.id), AccessState.terminated
            )

        assert result["external_rows_written"] == 0
        assert result["external_rows_deleted"] == 1
        assert _read_radusergroup(radius_db, "set-state-2") == []
        db_session.refresh(sub)
        assert sub.access_state == "terminated"

    def test_none_state_deletes_dotmac_rows(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        # status=pending → derive_access_state returns None
        sub, _ = _seed_subscription(
            db_session,
            subscriber,
            catalog_offer,
            username="set-state-3",
            status=SubscriptionStatus.pending,
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(
            radius_db, rows=[("set-state-3", "dotmac-suspended", 0)]
        )
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            set_subscription_access_state(db_session, str(sub.id), None)

        assert _read_radusergroup(radius_db, "set-state-3") == []
        db_session.refresh(sub)
        assert sub.access_state is None


class TestSetAccessStateIdempotency:
    def test_repeat_calls_keep_one_row(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        sub, _ = _seed_subscription(
            db_session, subscriber, catalog_offer, username="set-state-rep"
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            set_subscription_access_state(db_session, str(sub.id), AccessState.active)
            set_subscription_access_state(db_session, str(sub.id), AccessState.active)
            set_subscription_access_state(db_session, str(sub.id), AccessState.active)

        assert _read_radusergroup(radius_db, "set-state-rep") == [
            ("set-state-rep", "dotmac-active", 0)
        ]

    def test_state_transition_replaces_group(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        # First create as active, then transition by flipping the
        # subscription's status so the aggregate also flips.
        sub, _ = _seed_subscription(
            db_session, subscriber, catalog_offer, username="set-state-trans"
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            set_subscription_access_state(db_session, str(sub.id), AccessState.active)
            sub.status = SubscriptionStatus.suspended
            db_session.commit()
            set_subscription_access_state(
                db_session, str(sub.id), AccessState.suspended
            )

        # Captive redirect is opt-in; this subscriber didn't opt in, so the
        # suspended-status aggregate lands in dotmac-suspended (hard reject).
        assert _read_radusergroup(radius_db, "set-state-trans") == [
            ("set-state-trans", "dotmac-suspended", 0)
        ]


class TestSetAccessStatePreservesNonDotmacGroups:
    def test_operator_groups_outside_dotmac_namespace_are_kept(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        """The DELETE is scoped to groupname LIKE 'dotmac-%' so any
        operator-managed groups outside that namespace are preserved."""
        sub, _ = _seed_subscription(
            db_session, subscriber, catalog_offer, username="set-state-mix"
        )
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(
            radius_db,
            rows=[
                ("set-state-mix", "ops-custom-group", 5),
                ("set-state-mix", "dotmac-suspended", 0),  # stale
            ],
        )
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            set_subscription_access_state(db_session, str(sub.id), AccessState.active)

        rows = _read_radusergroup(radius_db, "set-state-mix")
        assert ("set-state-mix", "ops-custom-group", 5) in rows
        assert ("set-state-mix", "dotmac-active", 0) in rows
        assert ("set-state-mix", "dotmac-suspended", 0) not in rows


class TestSetAccessStateNoOps:
    def test_returns_zero_when_no_credentials(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        # Subscription exists but no AccessCredential for the subscriber.
        sub = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
        )
        db_session.add(sub)
        db_session.commit()
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            result = set_subscription_access_state(
                db_session, str(sub.id), AccessState.active
            )

        assert result["credentials"] == 0
        assert result["external_rows_written"] == 0
        assert result["external_rows_deleted"] == 0
        assert result["aggregate_state"] == "active"

    def test_returns_zero_when_no_external_configs(
        self, db_session, subscriber, catalog_offer
    ):
        sub, _ = _seed_subscription(
            db_session, subscriber, catalog_offer, username="set-state-noext"
        )
        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[],
        ):
            result = set_subscription_access_state(
                db_session, str(sub.id), AccessState.active
            )

        # App DB still updated, but no external rows possible.
        assert result["external_rows_written"] == 0
        db_session.refresh(sub)
        assert sub.access_state == "active"

    def test_missing_subscription_returns_skip(self, db_session):
        result = set_subscription_access_state(
            db_session, "00000000-0000-0000-0000-000000000000", AccessState.active
        )
        assert result == {
            "credentials": 0,
            "external_rows_written": 0,
            "external_rows_deleted": 0,
            "aggregate_state": None,
        }


class TestSubscriberAggregation:
    """When a subscriber has multiple subscriptions in different states,
    the radusergroup write must reflect the most-permissive state
    (active > captive > suspended > terminated)."""

    def test_active_plus_terminated_writes_active(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        # One active sub + one terminated sub for the same subscriber.
        sub_active, _ = _seed_subscription(
            db_session, subscriber, catalog_offer, username="agg-1"
        )
        sub_terminated = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
        )
        db_session.add(sub_terminated)
        db_session.commit()
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            # Even when we call set on the terminated sub LAST, the
            # subscriber-aggregate (active wins) keeps the dotmac-active row.
            set_subscription_access_state(
                db_session, str(sub_active.id), AccessState.active
            )
            result = set_subscription_access_state(
                db_session, str(sub_terminated.id), AccessState.terminated
            )

        assert result["aggregate_state"] == "active"
        assert _read_radusergroup(radius_db, "agg-1") == [("agg-1", "dotmac-active", 0)]

    def test_captive_plus_suspended_writes_captive(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        subscriber.captive_redirect_enabled = True
        db_session.commit()
        sub1, _ = _seed_subscription(
            db_session,
            subscriber,
            catalog_offer,
            username="agg-2",
            status=SubscriptionStatus.suspended,
        )
        sub2 = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.suspended,
        )
        db_session.add(sub2)
        db_session.commit()
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(radius_db)
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            # Both subs are suspended, but captive_redirect_enabled
            # promotes both to captive at the per-sub derive step. The
            # aggregate is also captive.
            set_subscription_access_state(db_session, str(sub1.id), AccessState.captive)

        assert _read_radusergroup(radius_db, "agg-2") == [
            ("agg-2", "dotmac-captive", 0)
        ]

    def test_all_terminated_writes_no_row(
        self, db_session, tmp_path, subscriber, catalog_offer
    ):
        sub1, _ = _seed_subscription(
            db_session,
            subscriber,
            catalog_offer,
            username="agg-3",
            status=SubscriptionStatus.canceled,
        )
        sub2 = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.expired,
        )
        db_session.add(sub2)
        db_session.commit()
        radius_db = tmp_path / "external.db"
        _write_radusergroup_sqlite(
            radius_db,
            rows=[("agg-3", "dotmac-active", 0)],  # stale
        )
        config = _fake_config(radius_db)

        with patch(
            "app.services.radius_access_state._active_external_sync_configs",
            return_value=[config],
        ):
            result = set_subscription_access_state(
                db_session, str(sub1.id), AccessState.terminated
            )

        assert result["aggregate_state"] == "terminated"
        # Both subs terminated → aggregate terminated → no row, stale wiped.
        assert _read_radusergroup(radius_db, "agg-3") == []
