from __future__ import annotations

import sqlite3

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.radius import RadiusSyncJob
from app.services import radius_population
from app.services.external_radius_targets import (
    ExternalRadiusTargetMismatch,
    active_external_radius_targets,
    assert_legacy_target_alignment,
    seed_external_radius_target_from_env,
    target_fingerprint,
)


def _target(path, *, name="primary", use_group=True, prefix=""):
    return {
        "target_name": name,
        "target_fingerprint": target_fingerprint(f"sqlite:///{path}"),
        "db_url": f"sqlite:///{path}",
        "radcheck_table": f"{prefix}radcheck",
        "radreply_table": f"{prefix}radreply",
        "radusergroup_table": f"{prefix}radusergroup",
        "nas_table": f"{prefix}nas",
        "password_attribute": "Cleartext-Password",
        "password_op": ":=",
        "default_reply_op": ":=",
        "use_group": use_group,
        "group_priority": 10,
    }


def _schema(path, *, prefix=""):
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"CREATE TABLE {prefix}radcheck "  # noqa: S608 - test-only identifier
            "(username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {prefix}radreply "  # noqa: S608 - test-only identifier
            "(username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {prefix}radusergroup "  # noqa: S608 - test-only identifier
            "(username TEXT, groupname TEXT, priority INTEGER)"
        )


def _read(path, table):
    with sqlite3.connect(path) as conn:
        return list(conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))  # noqa: S608


def test_owner_writes_auth_reply_and_both_configured_group_types(tmp_path):
    path = tmp_path / "radius.db"
    _schema(path)
    target = _target(path)
    work = [
        radius_population.RadiusProjectionWorkItem(
            username="customer-1",
            cleartext_password="secret",
            check_attrs=(("Simultaneous-Use", ":=", "1"),),
            reply_attrs=(("Framed-IP-Address", ":=", "10.0.0.1"),),
            blocked=False,
            status=SubscriptionStatus.active,
            mode="active",
            profile_group="fiber-profile",
        )
    ]

    engine = radius_population.get_external_engine(target["db_url"])
    for _ in range(2):
        with engine.begin() as conn:
            radius_population._write_radius_projection(
                conn,
                target,
                work,
                {"customer-1"},
                access_groups={
                    "active": "settings-active",
                    "suspended": "settings-suspended",
                    "captive": "settings-captive",
                },
                access_group_priority=20,
                group_routing_enabled=True,
            )

    assert _read(path, "radcheck") == [
        ("customer-1", "Cleartext-Password", ":=", "secret"),
        ("customer-1", "Simultaneous-Use", ":=", "1"),
    ]
    assert _read(path, "radreply") == [
        ("customer-1", "Framed-IP-Address", ":=", "10.0.0.1")
    ]
    assert _read(path, "radusergroup") == [
        ("customer-1", "fiber-profile", 10),
        ("customer-1", "settings-active", 20),
    ]


def test_owner_honours_per_target_table_settings_and_removal(tmp_path):
    path = tmp_path / "custom.db"
    _schema(path, prefix="customer_")
    target = _target(path, prefix="customer_", use_group=False)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO customer_radcheck VALUES (?, ?, ?, ?)",
            ("gone", "Cleartext-Password", ":=", "old"),
        )
        conn.execute(
            "INSERT INTO customer_radusergroup VALUES (?, ?, ?)",
            ("gone", "settings-active", 0),
        )

    engine = radius_population.get_external_engine(target["db_url"])
    with engine.begin() as conn:
        radius_population._write_radius_projection(
            conn,
            target,
            [],
            {"gone"},
            access_groups={
                "active": "settings-active",
                "suspended": "settings-suspended",
                "captive": "settings-captive",
            },
            access_group_priority=0,
            group_routing_enabled=False,
        )

    assert _read(path, "customer_radcheck") == []
    assert _read(path, "customer_radusergroup") == []


def test_owner_reject_replaces_password_reply_and_projects_suspended_group(tmp_path):
    path = tmp_path / "reject.db"
    _schema(path)
    target = _target(path, use_group=False)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO radcheck VALUES (?, ?, ?, ?)",
            ("blocked", "Cleartext-Password", ":=", "old"),
        )
        conn.execute(
            "INSERT INTO radreply VALUES (?, ?, ?, ?)",
            ("blocked", "Framed-IP-Address", ":=", "10.0.0.1"),
        )
    work = [
        radius_population.RadiusProjectionWorkItem(
            username="blocked",
            cleartext_password="unused",
            check_attrs=(("Simultaneous-Use", ":=", "1"),),
            reply_attrs=(),
            blocked=True,
            status=SubscriptionStatus.suspended,
            mode="reject",
            profile_group=None,
        )
    ]
    engine = radius_population.get_external_engine(target["db_url"])
    with engine.begin() as conn:
        radius_population._write_radius_projection(
            conn,
            target,
            work,
            {"blocked"},
            access_groups={
                "active": "settings-active",
                "suspended": "settings-suspended",
                "captive": "settings-captive",
            },
            access_group_priority=7,
            group_routing_enabled=True,
        )

    assert _read(path, "radcheck") == [("blocked", "Auth-Type", ":=", "Reject")]
    assert _read(path, "radreply") == []
    assert _read(path, "radusergroup") == [("blocked", "settings-suspended", 7)]


def test_fingerprint_contains_no_credentials(tmp_path):
    first = target_fingerprint("postgresql+psycopg://alice:first@radius/radius")
    second = target_fingerprint("postgresql+psycopg://bob:second@radius/radius")
    assert first == second
    assert "alice" not in first and "first" not in first


def test_cutover_rejects_equivalent_rows_on_a_different_database(
    tmp_path, db_session, monkeypatch
):
    legacy = tmp_path / "legacy.db"
    configured = tmp_path / "configured.db"
    for path in (legacy, configured):
        _schema(path)
    target = _target(configured)
    monkeypatch.setattr(
        "app.services.external_radius_targets.radius_dsn.resolve_radius_dsn",
        lambda: f"sqlite:///{legacy}",
    )
    monkeypatch.setattr(
        "app.services.external_radius_targets.active_external_radius_targets",
        lambda _db, capability=None: [target],
    )

    with pytest.raises(ExternalRadiusTargetMismatch, match="does not match"):
        assert_legacy_target_alignment(db_session)


def test_legacy_env_bootstraps_encrypted_db_config_once(db_session, monkeypatch):
    bootstrap_url = "sqlite:////tmp/radius-bootstrap-test.db"
    monkeypatch.setattr(
        "app.services.external_radius_targets.radius_dsn.resolve_radius_dsn",
        lambda: bootstrap_url,
    )
    monkeypatch.setattr(
        "app.services.settings_spec.resolve_value",
        lambda _db, _domain, key: {
            "default_auth_port": 1912,
            "default_acct_port": 1913,
        }.get(key),
    )

    assert seed_external_radius_target_from_env(db_session) is True
    assert seed_external_radius_target_from_env(db_session) is False

    targets = active_external_radius_targets(db_session, capability="users")
    assert len(targets) == 1
    assert targets[0]["db_url"] == bootstrap_url
    assert targets[0]["authoritative_accounting"] is True
    job = db_session.get(RadiusSyncJob, targets[0]["target_id"])
    assert job.server.auth_port == 1912
    assert job.server.acct_port == 1913
    assert job.connector_config.base_url is None
    assert job.connector_config.auth_config == {"db_url": bootstrap_url}
