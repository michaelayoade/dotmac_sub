"""Tests for the IPv4 connectivity reconciler (step 2, IP dimension).

Shadow-by-default: ``apply=False`` writes nothing; ``apply=True`` converges the
column to the IPAM truth and enqueues the single-writer refresh. Uses a sqlite
RADIUS stand-in, same pattern as test_ip_consistency_audit.py.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from app.models.catalog import AccessState, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.connectivity_reconciler import (
    connectivity_shadow_diff,
    converge_subscription_connectivity,
    current_write_source,
    derive_desired_connectivity,
    note_connectivity_write,
    plan_subscription_ip,
    reconciler_write_scope,
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
        assert plan["desired_ip"] == "10.0.0.9"
        assert plan["source"] == "column"
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
        with patch("app.tasks.radius_population.refresh_radius_from_subs") as task:
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
            patch("app.tasks.radius_population.refresh_radius_from_subs", fake_task),
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
        with patch("app.tasks.radius_population.refresh_radius_from_subs") as task:
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
        assert result["dimension"] == "suspend"
        assert result["applied"] is False
        assert sub.ipv4_address == "10.0.0.9"  # untouched


class TestDesiredConnectivity:
    """Pure transition table — CONNECTIVITY_STATE_MACHINE.md §2 invariants."""

    def test_active_full_access_no_kick(self):
        d = derive_desired_connectivity(SubscriptionStatus.active)
        assert d.access_state is AccessState.active
        assert d.credentials_active and d.ip_active and d.ip_retained
        assert d.kick_live_session is False

    def test_pending_unprovisioned(self):
        d = derive_desired_connectivity(SubscriptionStatus.pending)
        assert d.access_state is None
        assert not d.credentials_active and not d.ip_active and not d.ip_retained
        assert d.kick_live_session is False

    def test_hidden_and_archived_unprovisioned(self):
        for status in (SubscriptionStatus.hidden, SubscriptionStatus.archived):
            d = derive_desired_connectivity(status)
            assert d.access_state is None and not d.ip_active

    def test_blocked_family_retains_ip_and_creds(self):
        # INV-1 (IP retained across suspend) + INV-3 (creds survive suspend).
        for status in (
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        ):
            d = derive_desired_connectivity(status)
            # Default is suspended; walled-garden (captive) is opt-in per customer (#216).
            assert d.access_state is AccessState.suspended
            assert d.credentials_active is True
            assert d.ip_active is True and d.ip_retained is True
            assert d.kick_live_session is True  # INV-5: disconnect once

    def test_hard_reject_is_suspended_tier_still_retains_ip(self):
        d = derive_desired_connectivity(SubscriptionStatus.suspended, hard_reject=True)
        assert d.access_state is AccessState.suspended
        assert d.credentials_active is True
        assert d.ip_retained is True  # even hard block keeps the address (INV-1)

    def test_terminal_releases_everything(self):
        # INV-3/INV-4: creds inactive, IP released + cache cleared, kick once.
        for status in (
            SubscriptionStatus.canceled,
            SubscriptionStatus.expired,
            SubscriptionStatus.disabled,
        ):
            d = derive_desired_connectivity(status)
            assert d.access_state is AccessState.terminated
            assert d.credentials_active is False
            assert d.ip_active is False and d.ip_retained is False
            assert d.kick_live_session is True


class TestWriteSourceMarker:
    """The legacy-write detector must NOT flag reconciler-originated writes."""

    @staticmethod
    def _count(field, source):
        from app.metrics import CONNECTIVITY_DIRECT_WRITE

        return CONNECTIVITY_DIRECT_WRITE.labels(field=field, source=source)._value.get()

    def test_default_source_is_legacy(self):
        assert current_write_source() == "legacy"

    def test_scope_marks_reconciler_and_restores(self):
        with reconciler_write_scope():
            assert current_write_source() == "reconciler"
        assert current_write_source() == "legacy"

    def test_note_attributes_to_correct_source(self):
        field = "subscription.ipv4_address"
        before_legacy = self._count(field, "legacy")
        before_recon = self._count(field, "reconciler")

        note_connectivity_write(field, "test_legacy_caller")  # outside scope
        with reconciler_write_scope():
            note_connectivity_write(field, "test_reconciler_caller")

        assert self._count(field, "legacy") == before_legacy + 1
        assert self._count(field, "reconciler") == before_recon + 1


class TestShadowDiff:
    def test_active_unprovisioned_flags_access_and_credentials(
        self, db_session, catalog_offer
    ):
        # Active sub, no access_state set, no credentials, but IP present.
        sub = _seed_sub(
            db_session,
            login="sd1",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.5",
            assign_ip="10.0.0.5",
        )
        report = connectivity_shadow_diff(db_session, sub.subscriber_id)
        assert "access_state" in report["diffs"]  # desired active, actual None
        assert "credentials_active" in report["diffs"]  # desired True, none exist
        assert "ip_active" not in report["diffs"]  # active IPAssignment present
        assert report["ip"]["match"] is True

    def test_terminal_consistent_has_no_diffs(self, db_session, catalog_offer):
        sub = _seed_sub(
            db_session,
            login="sd2",
            offer=catalog_offer,
            status=SubscriptionStatus.canceled,
        )
        sub.access_state = AccessState.terminated.value  # actual matches desired
        db_session.commit()
        report = connectivity_shadow_diff(db_session, sub.subscriber_id)
        # Terminal desires no creds, no IP, terminated access — and the seed has
        # exactly that, so nothing disagrees.
        assert report["diffs"] == []
        assert report["credentials"]["match"] and report["ip"]["match"]


class TestShadowDiffIpv4Cache:
    """The ipv4_cache dimension (INV-4 / R2): served column must equal the
    active assignment IP when an IP is retained. This gauge sizes the cutover
    that removes the accounting dual-write into the served column."""

    def test_diff_when_served_column_differs_from_assignment(
        self, db_session, catalog_offer
    ):
        sub = _seed_sub(
            db_session,
            login="cache1",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.9",  # served column
            assign_ip="10.0.0.5",  # active assignment (source of truth)
        )
        report = connectivity_shadow_diff(db_session, sub.subscriber_id)
        assert "ipv4_cache" in report["diffs"]
        assert report["ipv4_cache"]["match"] is False

    def test_no_diff_when_served_column_matches_assignment(
        self, db_session, catalog_offer
    ):
        sub = _seed_sub(
            db_session,
            login="cache2",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.5",
            assign_ip="10.0.0.5",
        )
        report = connectivity_shadow_diff(db_session, sub.subscriber_id)
        assert "ipv4_cache" not in report["diffs"]
        assert report["ipv4_cache"]["match"] is True

    def test_terminal_sub_does_not_flag_ipv4_cache(self, db_session, catalog_offer):
        # canceled = ip not retained → the column is irrelevant, no cache diff
        sub = _seed_sub(
            db_session,
            login="cache3",
            offer=catalog_offer,
            status=SubscriptionStatus.canceled,
            col_ip="10.0.0.9",
            assign_ip=None,
        )
        report = connectivity_shadow_diff(db_session, sub.subscriber_id)
        assert "ipv4_cache" not in report["diffs"]


class TestAccountingObservedIpSplit:
    """§3.1: the live framed IP from accounting goes to last_seen_framed_*,
    NOT the served-IP column (except the retained legacy dual-write for ACTIVE
    subs)."""

    def test_observed_ip_recorded_to_last_seen_and_spares_served_when_inactive(
        self, db_session, catalog_offer
    ):
        from app.services.usage import _write_subscription_ips_from_accounting

        sub = _seed_sub(
            db_session,
            login="obs1",
            offer=catalog_offer,
            status=SubscriptionStatus.suspended,
            col_ip="10.0.0.5",
        )
        _write_subscription_ips_from_accounting(
            db_session, sub.id, ipv4="10.0.0.99", ipv6=None
        )
        assert sub.last_seen_framed_ipv4 == "10.0.0.99"  # observed recorded
        assert sub.ipv4_address == "10.0.0.5"  # served column untouched (suspended)

    def test_active_sub_still_dual_writes_served_column(
        self, db_session, catalog_offer
    ):
        from app.services.usage import _write_subscription_ips_from_accounting

        sub = _seed_sub(
            db_session,
            login="obs2",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.5",
        )
        _write_subscription_ips_from_accounting(
            db_session, sub.id, ipv4="10.0.0.99", ipv6=None
        )
        assert sub.last_seen_framed_ipv4 == "10.0.0.99"
        assert sub.ipv4_address == "10.0.0.99"  # legacy dual-write retained


class TestConnectivityShadowAudit:
    """Full-base sweep aggregates per-dimension drift across subscribers."""

    def test_sweep_counts_ipv4_cache_drift_and_population(
        self, db_session, catalog_offer
    ):
        from app.services.connectivity_reconciler import connectivity_shadow_audit

        # drifting: served column != active assignment
        _seed_sub(
            db_session,
            login="sweep_drift",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.5",
        )
        # clean ipv4_cache: column == assignment (distinct IP from the other sub
        # — IPv4Address.address is unique)
        _seed_sub(
            db_session,
            login="sweep_clean",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
            col_ip="10.0.0.6",
            assign_ip="10.0.0.6",
        )
        result = connectivity_shadow_audit(db_session)
        assert result["population"] == 2  # both are connectivity-retaining
        assert result["counts"]["ipv4_cache"] == 1
        assert len(result["samples"]["ipv4_cache"]) == 1

    def test_terminal_subs_excluded_from_population(self, db_session, catalog_offer):
        from app.services.connectivity_reconciler import connectivity_shadow_audit

        _seed_sub(
            db_session,
            login="sweep_canceled",
            offer=catalog_offer,
            status=SubscriptionStatus.canceled,
            col_ip="10.0.0.9",
        )
        result = connectivity_shadow_audit(db_session)
        assert result["population"] == 0  # canceled is not connectivity-retaining
