"""Tests for the IPv4 consistency audit (step 1 of the reconciler hardening).

Compares the three IPv4 sources for an active subscription — the
subscription.ipv4_address column, the IPAM IPAssignment, and the external
radreply Framed-IP — against a sqlite RADIUS stand-in (same pattern as
test_radius_reconciliation.py). See
docs/FINANCIAL_ACCESS_ENFORCEMENT.md.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.ip_consistency_audit import audit_ip_consistency


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
        "radusergroup_table": "radusergroup",
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
        first_name="Audit",
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


def _run(db_session, db_path):
    with patch(
        "app.services.ip_consistency_audit._active_external_sync_configs",
        return_value=[_fake_config(db_path)],
    ):
        return audit_ip_consistency(db_session)


class TestIpConsistencyAudit:
    def test_clean_when_all_three_agree(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-a", "Cleartext-Password", "pw")],
            radreply=[("user-a", "Framed-IP-Address", "10.0.0.5")],
        )
        _seed_sub(
            db_session,
            login="user-a",
            offer=catalog_offer,
            col_ip="10.0.0.5",
            assign_ip="10.0.0.5",
        )
        result = _run(db_session, db_path)
        assert result["ok"] is True
        assert result["population"] == 1
        assert result["counts"] == dict.fromkeys(result["counts"], 0)

    def test_dynamic_sub_excluded(self, db_session, tmp_path, catalog_offer):
        """Active sub with no IP in any source is dynamic — not drift."""
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(db_path, radcheck=[("dyn", "Cleartext-Password", "pw")])
        _seed_sub(db_session, login="dyn", offer=catalog_offer)
        result = _run(db_session, db_path)
        assert result["population"] == 0
        assert result["ok"] is True

    def test_assignment_missing(self, db_session, tmp_path, catalog_offer):
        """Column IP set but no IPAM row backs it — the core R2 metric."""
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-b", "Cleartext-Password", "pw")],
            radreply=[("user-b", "Framed-IP-Address", "10.0.0.6")],
        )
        _seed_sub(db_session, login="user-b", offer=catalog_offer, col_ip="10.0.0.6")
        result = _run(db_session, db_path)
        assert result["counts"]["assignment_missing"] == 1
        assert result["assignment_missing"] == ["user-b"]
        assert result["ok"] is False

    def test_assignment_mismatch(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-c", "Cleartext-Password", "pw")],
            radreply=[("user-c", "Framed-IP-Address", "10.0.0.7")],
        )
        _seed_sub(
            db_session,
            login="user-c",
            offer=catalog_offer,
            col_ip="10.0.0.7",
            assign_ip="10.0.0.99",
        )
        result = _run(db_session, db_path)
        assert result["counts"]["assignment_mismatch"] == 1
        assert result["counts"]["assignment_missing"] == 0

    def test_radreply_missing(self, db_session, tmp_path, catalog_offer):
        """Provisioned login, column IP set, but no Framed-IP in radreply."""
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-d", "Cleartext-Password", "pw")],
        )
        _seed_sub(
            db_session,
            login="user-d",
            offer=catalog_offer,
            col_ip="10.0.0.8",
            assign_ip="10.0.0.8",
        )
        result = _run(db_session, db_path)
        assert result["counts"]["radreply_missing"] == 1

    def test_intentional_reject_does_not_require_radreply(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-reject", "Auth-Type", "Reject")],
        )
        sub = _seed_sub(
            db_session,
            login="user-reject",
            offer=catalog_offer,
            col_ip="10.0.0.18",
            assign_ip="10.0.0.18",
        )
        projection = SimpleNamespace(
            subscription_id=str(sub.id),
            plan=SimpleNamespace(write_radreply=False),
        )
        with patch(
            "app.services.radius_projection_planner.plan_login_radius_projections",
            return_value={"user-reject": projection},
        ):
            result = _run(db_session, db_path)
        assert result["counts"]["radreply_missing"] == 0

    def test_radreply_mismatch(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-e", "Cleartext-Password", "pw")],
            radreply=[("user-e", "Framed-IP-Address", "10.0.0.50")],
        )
        _seed_sub(
            db_session,
            login="user-e",
            offer=catalog_offer,
            col_ip="10.0.0.9",
            assign_ip="10.0.0.9",
        )
        result = _run(db_session, db_path)
        assert result["counts"]["radreply_mismatch"] == 1

    def test_radreply_orphan(self, db_session, tmp_path, catalog_offer):
        """RADIUS pins an IP the system no longer tracks (column empty)."""
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("user-f", "Cleartext-Password", "pw")],
            radreply=[("user-f", "Framed-IP-Address", "10.0.0.40")],
        )
        _seed_sub(db_session, login="user-f", offer=catalog_offer)
        result = _run(db_session, db_path)
        assert result["counts"]["radreply_orphan"] == 1
        assert result["population"] == 1

    def test_suspended_sub_not_in_population(self, db_session, tmp_path, catalog_offer):
        """Only active subs are audited — a suspended sub (radreply deleted by
        design) must not be flagged as radreply_missing."""
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(db_path, radcheck=[("user-g", "Auth-Type", "Reject")])
        _seed_sub(
            db_session,
            login="user-g",
            offer=catalog_offer,
            status=SubscriptionStatus.suspended,
            col_ip="10.0.0.10",
            assign_ip="10.0.0.10",
        )
        result = _run(db_session, db_path)
        assert result["population"] == 0
        assert result["ok"] is True

    def test_sibling_assignment_cannot_create_subscriber_level_false_mismatch(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("sibling-a", "Cleartext-Password", "pw")],
            radreply=[("sibling-a", "Framed-IP-Address", "10.0.0.21")],
        )
        subscriber = Subscriber(
            first_name="Sibling",
            last_name="Audit",
            email="sibling-audit@example.com",
        )
        db_session.add(subscriber)
        db_session.flush()
        first = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="sibling-a",
            ipv4_address="10.0.0.21",
        )
        second = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="sibling-b",
            ipv4_address="10.0.0.22",
        )
        db_session.add_all([first, second])
        db_session.flush()
        for subscription, address_value in (
            (first, "10.0.0.21"),
            (second, "10.0.0.22"),
        ):
            address = IPv4Address(address=address_value)
            db_session.add(address)
            db_session.flush()
            db_session.add(
                IPAssignment(
                    subscriber_id=subscriber.id,
                    subscription_id=subscription.id,
                    ip_version=IPVersion.ipv4,
                    ipv4_address_id=address.id,
                    is_active=True,
                )
            )
        db_session.commit()

        result = _run(db_session, db_path)
        assert result["counts"]["assignment_mismatch"] == 0

    def test_no_external_config_reports_error(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_sub(db_session, login="user-h", offer=catalog_offer, col_ip="10.0.0.11")
        with patch(
            "app.services.ip_consistency_audit._active_external_sync_configs",
            return_value=[],
        ):
            result = audit_ip_consistency(db_session)
        # assignment_missing still detected from app DB; radreply checks skipped.
        assert result["errors"] == 1
        assert result["ok"] is False


class TestStorageAndCollector:
    def _fake_redis(self):
        class FakeRedis:
            def __init__(self):
                self.store = {}

            def set(self, key, value, ex=None):
                self.store[key] = value

            def get(self, key):
                return self.store.get(key)

        return FakeRedis()

    def test_store_and_load_roundtrip(self):
        from app.services import ip_consistency_audit as ica

        fake = self._fake_redis()
        with patch.object(ica, "_get_redis", return_value=fake):
            assert ica.store_latest_ip_audit(
                {"counts": {"assignment_missing": 4}, "population": 100}
            )
            loaded = ica.load_latest_ip_audit()
        assert loaded["counts"] == {"assignment_missing": 4}
        assert loaded["population"] == 100
        assert "ran_at" in loaded

    def test_collector_exports_counts_and_population(self):
        from datetime import UTC, datetime

        from app.metrics import _IpConsistencyAuditCollector

        data = {
            "counts": {"assignment_missing": 2, "radreply_mismatch": 1},
            "population": 50,
            "ran_at": datetime.now(UTC).isoformat(),
        }
        with patch(
            "app.services.ip_consistency_audit.load_latest_ip_audit",
            return_value=data,
        ):
            families = list(_IpConsistencyAuditCollector().collect())
        by_name = {f.name: f for f in families}
        drift = by_name["radius_ip_consistency_drift"]
        samples = {s.labels["kind"]: s.value for s in drift.samples}
        assert samples["assignment_missing"] == 2
        assert samples["radreply_mismatch"] == 1
        assert by_name["radius_ip_consistency_population"].samples[0].value == 50

    def test_collector_silent_without_data(self):
        from app.metrics import _IpConsistencyAuditCollector

        with patch(
            "app.services.ip_consistency_audit.load_latest_ip_audit",
            return_value=None,
        ):
            assert list(_IpConsistencyAuditCollector().collect()) == []


class TestCollisionClasses:
    """Cross-service collision kinds (tactical stopgap, pre-dotmac-ipam)."""

    def _empty_radius(self, tmp_path):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(db_path)
        return db_path

    def test_same_ip_on_two_live_subscriptions(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_sub(db_session, login="dup-a", offer=catalog_offer, col_ip="198.51.100.5")
        _seed_sub(db_session, login="dup-b", offer=catalog_offer, col_ip="198.51.100.5")
        _seed_sub(
            db_session,
            login="gone",
            offer=catalog_offer,
            status=SubscriptionStatus.canceled,
            col_ip="198.51.100.9",
        )
        _seed_sub(
            db_session,
            login="gone2",
            offer=catalog_offer,
            status=SubscriptionStatus.canceled,
            col_ip="198.51.100.9",
        )
        result = _run(db_session, self._empty_radius(tmp_path))
        assert result["ip_on_multiple_subscriptions"] == ["198.51.100.5"]
        assert result["counts"]["ip_on_multiple_subscriptions"] == 1
        assert result["ok"] is False

    def test_active_assignment_on_terminal_subscription(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_sub(
            db_session,
            login="term",
            offer=catalog_offer,
            status=SubscriptionStatus.expired,
            assign_ip="198.51.100.20",
        )
        result = _run(db_session, self._empty_radius(tmp_path))
        assert result["assignment_on_terminal_subscription"] == ["198.51.100.20"]

    def test_session_ip_mismatch_duplicate_and_unlinked_pool(
        self, db_session, tmp_path, catalog_offer
    ):
        from datetime import UTC, datetime

        from app.models.catalog import NasDevice
        from app.models.network import IpPool
        from app.models.radius_active_session import RadiusActiveSession

        nas = NasDevice(
            name="collision-nas", is_active=True, tags=["radius_pool:other"]
        )
        pool = IpPool(
            name="collision-pool",
            ip_version=IPVersion.ipv4,
            cidr="198.51.100.0/24",
            is_active=True,
        )
        db_session.add_all([nas, pool])
        db_session.flush()
        sub_a = _seed_sub(
            db_session, login="s-a", offer=catalog_offer, assign_ip="198.51.100.30"
        )
        sub_b = _seed_sub(db_session, login="s-b", offer=catalog_offer)
        db_session.query(IPv4Address).filter(
            IPv4Address.address == "198.51.100.30"
        ).update({"pool_id": pool.id})
        for sub, framed, sid in (
            (sub_a, "198.51.100.31", "1"),  # online on an IP != IPAM (.30)
            (sub_b, "198.51.100.31", "2"),  # same framed IP, second session
        ):
            db_session.add(
                RadiusActiveSession(
                    subscription_id=sub.id,
                    subscriber_id=sub.subscriber_id,
                    nas_device_id=nas.id,
                    username=f"u{sid}",
                    acct_session_id=sid,
                    framed_ip_address=framed,
                    session_start=datetime.now(UTC),
                )
            )
        db_session.add(IPv4Address(address="198.51.100.31", pool_id=pool.id))
        db_session.commit()

        radius = self._empty_radius(tmp_path)
        result = _run(db_session, radius)
        # No other subscriber holds .31 in IPAM: stale hygiene, not collision.
        assert result["session_ip_ipam_mismatch"] == []
        assert result["session_ip_ipam_stale"] == ["198.51.100.31"]
        assert result["session_ip_duplicate"] == ["198.51.100.31"]
        assert result["session_ip_pool_not_linked"] == ["198.51.100.31"]

        # Linking the pool to the NAS clears only the pool class.
        nas.tags = [f"radius_pool:{pool.id}"]
        db_session.commit()
        result = _run(db_session, radius)
        assert result["session_ip_pool_not_linked"] == []
        assert result["session_ip_duplicate"] == ["198.51.100.31"]


class TestCollisionPrecision:
    """Collision kinds mean real cross-customer conflict (documentation IPs)."""

    def _radius(self, tmp_path):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(db_path)
        return db_path

    def _add_sub(self, db_session, subscriber, offer, login, status, ip):
        sub = Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer.id,
            status=status,
            login=login,
            ipv4_address=ip,
        )
        db_session.add(sub)
        db_session.flush()
        return sub

    def _subscriber(self, db_session, tag):
        subscriber = Subscriber(
            first_name="P", last_name=tag, email=f"{tag}@example.com"
        )
        db_session.add(subscriber)
        db_session.flush()
        return subscriber

    def test_cross_subscriber_servable_is_critical_kind(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_sub(db_session, login="x-a", offer=catalog_offer, col_ip="203.0.113.5")
        _seed_sub(db_session, login="x-b", offer=catalog_offer, col_ip="203.0.113.5")
        result = _run(db_session, self._radius(tmp_path))
        assert result["ip_on_multiple_subscriptions"] == ["203.0.113.5"]
        assert result["counts"]["ip_shared_within_subscriber"] == 0
        assert (
            len(
                result["sample_subscribers"]["ip_on_multiple_subscriptions"][
                    "203.0.113.5"
                ]
            )
            >= 1
        )

    def test_same_subscriber_is_not_a_collision(
        self, db_session, tmp_path, catalog_offer
    ):
        owner = self._subscriber(db_session, "own")
        self._add_sub(
            db_session,
            owner,
            catalog_offer,
            "w-a",
            SubscriptionStatus.active,
            "203.0.113.6",
        )
        self._add_sub(
            db_session,
            owner,
            catalog_offer,
            "w-b",
            SubscriptionStatus.active,
            "203.0.113.6",
        )
        db_session.commit()
        result = _run(db_session, self._radius(tmp_path))
        assert result["counts"]["ip_on_multiple_subscriptions"] == 0
        assert result["ip_shared_within_subscriber"] == ["203.0.113.6"]
        assert result["counts"]["ip_column_reuse_historic"] == 1

    def test_disabled_historic_ip_reuse_is_informational_only(
        self, db_session, tmp_path, catalog_offer
    ):
        for login, status in (
            ("h-act", SubscriptionStatus.active),
            ("h-dis", SubscriptionStatus.disabled),
            ("h-hid", SubscriptionStatus.hidden),
            ("h-arc", SubscriptionStatus.archived),
        ):
            _seed_sub(
                db_session,
                login=login,
                offer=catalog_offer,
                status=status,
                col_ip="203.0.113.7",
            )
        result = _run(db_session, self._radius(tmp_path))
        assert result["counts"]["ip_on_multiple_subscriptions"] == 0
        assert result["counts"]["ip_shared_within_subscriber"] == 0
        assert result["ip_column_reuse_historic"] == ["203.0.113.7"]

    def test_informational_kinds_do_not_flip_ok(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_sub(
            db_session,
            login="i-a",
            offer=catalog_offer,
            status=SubscriptionStatus.disabled,
            col_ip="203.0.113.8",
        )
        _seed_sub(
            db_session,
            login="i-b",
            offer=catalog_offer,
            status=SubscriptionStatus.disabled,
            col_ip="203.0.113.8",
        )
        result = _run(db_session, self._radius(tmp_path))
        assert result["counts"]["ip_column_reuse_historic"] == 1
        assert result["ok"] is True

    def _session(self, db_session, nas, sub, ip, sid):
        from datetime import UTC, datetime

        from app.models.radius_active_session import RadiusActiveSession

        db_session.add(
            RadiusActiveSession(
                subscription_id=sub.id,
                subscriber_id=sub.subscriber_id,
                nas_device_id=nas.id,
                username=f"u{sid}",
                acct_session_id=sid,
                framed_ip_address=ip,
                session_start=datetime.now(UTC),
            )
        )
        db_session.commit()

    def _nas(self, db_session):
        from app.models.catalog import NasDevice

        nas = NasDevice(name="prec-nas", is_active=True)
        db_session.add(nas)
        db_session.flush()
        return nas

    def test_session_on_stale_own_assignment_is_hygiene_not_collision(
        self, db_session, tmp_path, catalog_offer
    ):
        nas = self._nas(db_session)
        sub = _seed_sub(
            db_session, login="st-a", offer=catalog_offer, assign_ip="203.0.113.20"
        )
        self._session(db_session, nas, sub, "203.0.113.21", "1")
        result = _run(db_session, self._radius(tmp_path))
        assert result["session_ip_ipam_stale"] == ["203.0.113.21"]
        assert result["counts"]["session_ip_ipam_mismatch"] == 0

    def test_session_on_other_subscribers_ip_is_critical(
        self, db_session, tmp_path, catalog_offer
    ):
        nas = self._nas(db_session)
        sub = _seed_sub(
            db_session, login="cr-a", offer=catalog_offer, assign_ip="203.0.113.30"
        )
        _seed_sub(
            db_session, login="cr-b", offer=catalog_offer, assign_ip="203.0.113.31"
        )
        self._session(db_session, nas, sub, "203.0.113.31", "1")
        result = _run(db_session, self._radius(tmp_path))
        assert result["session_ip_ipam_mismatch"] == ["203.0.113.31"]
        assert result["counts"]["session_ip_ipam_stale"] == 0

    def test_session_on_sibling_service_ip_of_same_subscriber_is_stale(
        self, db_session, tmp_path, catalog_offer
    ):
        nas = self._nas(db_session)
        owner = self._subscriber(db_session, "sib")
        a = self._add_sub(
            db_session, owner, catalog_offer, "sb-a", SubscriptionStatus.active, None
        )
        b = self._add_sub(
            db_session,
            owner,
            catalog_offer,
            "sb-b",
            SubscriptionStatus.active,
            "203.0.113.41",
        )
        for sub, ip in ((a, "203.0.113.40"), (b, "203.0.113.41")):
            addr = IPv4Address(address=ip)
            db_session.add(addr)
            db_session.flush()
            db_session.add(
                IPAssignment(
                    subscriber_id=owner.id,
                    subscription_id=sub.id,
                    ip_version=IPVersion.ipv4,
                    ipv4_address_id=addr.id,
                    is_active=True,
                )
            )
        db_session.commit()
        self._session(db_session, nas, a, "203.0.113.41", "1")
        result = _run(db_session, self._radius(tmp_path))
        assert result["counts"]["session_ip_ipam_mismatch"] == 0
        assert result["session_ip_ipam_stale"] == ["203.0.113.41"]


def test_summary_formatter_prints_counts_tiers_and_subscriber_numbers():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "one_off"
        / "audit_ip_consistency.py"
    )
    spec = importlib.util.spec_from_file_location("audit_ip_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {
        "population": 3,
        "errors": 0,
        "ok": False,
        "counts": {"ip_on_multiple_subscriptions": 1, "ip_column_reuse_historic": 9},
        "ip_on_multiple_subscriptions": ["203.0.113.5"],
        "ip_column_reuse_historic": ["203.0.113.9"],
        "sample_subscribers": {
            "ip_on_multiple_subscriptions": {"203.0.113.5": ["100001", "100002"]}
        },
    }
    out = module.format_summary(result)
    assert "ip_on_multiple_subscriptions" in out and "[CRITICAL]" in out
    assert "ip_column_reuse_historic" in out and "[info]" in out
    assert "203.0.113.5  subscribers=100001,100002" in out
