"""Tests for the IPv4 connectivity reconciler (step 2, IP dimension).

Shadow-by-default: ``apply=False`` writes nothing; ``apply=True`` converges the
column to the IPAM truth and enqueues the single-writer refresh. Uses a sqlite
RADIUS stand-in, same pattern as test_ip_consistency_audit.py.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.connectivity_reconciler import (
    converge_subscription_connectivity,
    plan_subscription_ip,
)


def _seed_radius_sqlite(db_path, *, radcheck=(), radreply=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radreply (username TEXT, attribute TEXT, value TEXT)"
        )
        conn.executemany("INSERT INTO radcheck VALUES (?, ?, ?)", radcheck)
        conn.executemany("INSERT INTO radreply VALUES (?, ?, ?)", radreply)
        conn.commit()
    finally:
        conn.close()


def _fake_config(db_path):
    return {
        "db_url": f"sqlite:///{db_path}",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
    }


def _seed_sub(
    db_session,
    *,
    login,
    offer,
    status=SubscriptionStatus.active,
    col_ip=None,
    assign_ip=None,
):
    subscriber = Subscriber(
        first_name="Recon",
        last_name="Case",
        email=f"{login}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=login,
        ipv4_address=col_ip,
    )
    db_session.add(sub)
    db_session.flush()
    if assign_ip is not None:
        addr = IPv4Address(address=assign_ip)
        db_session.add(addr)
        db_session.flush()
        db_session.add(
            IPAssignment(
                subscriber_id=subscriber.id,
                subscription_id=sub.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=addr.id,
                is_active=True,
            )
        )
    db_session.commit()
    return sub


def _converge(db_session, sub_id, db_path, *, apply):
    with patch(
        "app.services.ip_consistency_audit._active_external_sync_configs",
        return_value=[_fake_config(db_path)],
    ):
        return converge_subscription_connectivity(db_session, str(sub_id), apply=apply)


class TestPlan:
    def test_in_sync_no_actions(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u1", "Cleartext-Password", "pw")],
            radreply=[("u1", "Framed-IP-Address", "10.0.0.5")],
        )
        sub = _seed_sub(
            db_session,
            login="u1",
            offer=catalog_offer,
            col_ip="10.0.0.5",
            assign_ip="10.0.0.5",
        )
        with patch(
            "app.services.ip_consistency_audit._active_external_sync_configs",
            return_value=[_fake_config(db_path)],
        ):
            plan = plan_subscription_ip(db_session, sub)
        assert plan["actions"] == []
        assert plan["desired_ip"] == "10.0.0.5"
        assert plan["source"] == "ipam"

    def test_mismatch_is_adjudicate_by_default(
        self, db_session, tmp_path, catalog_offer
    ):
        """Default (trust_ipam=False): a column/IPAM mismatch is report-only —
        we will NOT change the served IP until IPAM is trusted (step 2a)."""
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u2", "Cleartext-Password", "pw")],
            radreply=[("u2", "Framed-IP-Address", "10.0.0.9")],
        )
        sub = _seed_sub(
            db_session,
            login="u2",
            offer=catalog_offer,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.7",
        )
        with patch(
            "app.services.ip_consistency_audit._active_external_sync_configs",
            return_value=[_fake_config(db_path)],
        ):
            plan = plan_subscription_ip(db_session, sub)
        kinds = {a["kind"] for a in plan["actions"]}
        assert "mismatch_adjudicate" in kinds
        assert "set_column" not in kinds

    def test_trust_ipam_enables_set_column(self, db_session, tmp_path, catalog_offer):
        """trust_ipam=True: column ← IPAM truth becomes applicable."""
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u2", "Cleartext-Password", "pw")],
            radreply=[("u2", "Framed-IP-Address", "10.0.0.9")],
        )
        sub = _seed_sub(
            db_session,
            login="u2",
            offer=catalog_offer,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.7",
        )
        with patch(
            "app.services.ip_consistency_audit._active_external_sync_configs",
            return_value=[_fake_config(db_path)],
        ):
            plan = plan_subscription_ip(db_session, sub, trust_ipam=True)
        kinds = {a["kind"] for a in plan["actions"]}
        assert plan["desired_ip"] == "10.0.0.7"
        assert "set_column" in kinds
        assert "refresh_radius" in kinds  # radreply 10.0.0.9 != desired 10.0.0.7

    def test_assignment_missing_is_report_only(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u3", "Cleartext-Password", "pw")],
            radreply=[("u3", "Framed-IP-Address", "10.0.0.8")],
        )
        sub = _seed_sub(db_session, login="u3", offer=catalog_offer, col_ip="10.0.0.8")
        with patch(
            "app.services.ip_consistency_audit._active_external_sync_configs",
            return_value=[_fake_config(db_path)],
        ):
            plan = plan_subscription_ip(db_session, sub)
        backfill = [a for a in plan["actions"] if a["kind"] == "backfill_ipam"]
        assert backfill and backfill[0]["note"] == "report-only"


class TestConverge:
    def test_shadow_writes_nothing(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u4", "Cleartext-Password", "pw")],
            radreply=[("u4", "Framed-IP-Address", "10.0.0.9")],
        )
        sub = _seed_sub(
            db_session,
            login="u4",
            offer=catalog_offer,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.7",
        )
        with patch("app.tasks.splynx_sync.run_refresh_radius_from_subs") as task:
            result = _converge(db_session, sub.id, db_path, apply=False)
        db_session.refresh(sub)
        assert result["applied"] is False
        assert sub.ipv4_address == "10.0.0.9"  # unchanged
        task.delay.assert_not_called()

    def test_apply_sets_column_and_enqueues_refresh(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u5", "Cleartext-Password", "pw")],
            radreply=[("u5", "Framed-IP-Address", "10.0.0.9")],
        )
        sub = _seed_sub(
            db_session,
            login="u5",
            offer=catalog_offer,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.7",
        )
        fake_task = MagicMock()
        with (
            patch("app.tasks.splynx_sync.run_refresh_radius_from_subs", fake_task),
            patch(
                "app.services.ip_consistency_audit._active_external_sync_configs",
                return_value=[_fake_config(db_path)],
            ),
        ):
            result = converge_subscription_connectivity(
                db_session, str(sub.id), apply=True, trust_ipam=True
            )
        db_session.refresh(sub)
        assert sub.ipv4_address == "10.0.0.7"  # converged to IPAM truth
        assert result["applied"] is True
        assert "set_column" in result["applied_actions"]
        assert "refresh_radius" in result["applied_actions"]
        fake_task.delay.assert_called_once()

    def test_apply_idempotent_when_in_sync(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("u6", "Cleartext-Password", "pw")],
            radreply=[("u6", "Framed-IP-Address", "10.0.0.7")],
        )
        sub = _seed_sub(
            db_session,
            login="u6",
            offer=catalog_offer,
            col_ip="10.0.0.7",
            assign_ip="10.0.0.7",
        )
        with patch("app.tasks.splynx_sync.run_refresh_radius_from_subs") as task:
            result = _converge(db_session, sub.id, db_path, apply=True)
        assert result["applied"] is False
        assert result["applied_actions"] == []
        task.delay.assert_not_called()

    def test_suspended_sub_is_noop(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "r.db"
        _seed_radius_sqlite(db_path)
        sub = _seed_sub(
            db_session,
            login="u7",
            offer=catalog_offer,
            status=SubscriptionStatus.suspended,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.7",
        )
        result = _converge(db_session, sub.id, db_path, apply=True)
        db_session.refresh(sub)
        assert result["reason"] == "not_active"
        assert sub.ipv4_address == "10.0.0.9"  # untouched
