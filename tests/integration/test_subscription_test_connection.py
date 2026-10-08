"""Real migration-owned grant constraints and PostgreSQL RADIUS deadline queries."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import insert, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from app.models.test_connection import TestConnectionGrant as ConnectionGrant
from app.services import test_connection as owner
from scripts.ci import template_database
from tests import test_subscription_test_connection as grant_tests

test_service = grant_tests.test_service


@pytest.fixture
def fresh_migration_database(template_base_url: URL, monkeypatch) -> Iterator[URL]:
    """Replay the migration path on a new disposable database, without cloning."""
    from app import config as app_config

    name = "dotmac_test_connection_migration_" + uuid4().hex
    maintenance = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        maintenance.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = template_base_url.set(database=name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        template_database.bootstrap_database_local_prerequisites(target)
        yield target
    finally:
        template_database.drop_database(template_base_url, name)


def test_migrated_grant_timer_and_audit_are_committed_together(
    db_session, test_service
):
    result = owner.activate_test_connection(db_session, command=test_service)
    row = db_session.get(ConnectionGrant, result.grant_id)
    assert row is not None
    assert row.expires_at == result.expires_at
    from sqlalchemy import select

    from app.models.durable_timer import DurableTimer

    timer = db_session.scalar(
        select(DurableTimer).where(DurableTimer.entity_id == row.id)
    )
    assert timer.due_at == result.expires_at


def test_migrated_unique_index_rejects_two_open_grants(db_session, test_service):
    first = owner.activate_test_connection(db_session, command=test_service)
    row = db_session.get(ConnectionGrant, first.grant_id)
    values = {
        "id": uuid4(),
        "command_id": uuid4(),
        "subscription_id": row.subscription_id,
        "subscriber_id": row.subscriber_id,
        "actor_id": row.actor_id,
        "actor_label": row.actor_label,
        "activated_at": row.activated_at,
        "expires_at": row.expires_at,
        "duration_seconds": 7200,
        "delivery_state": "pending",
    }
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(insert(ConnectionGrant).values(**values))


def _radius_query(name: str, *, schema: str) -> str:
    source = Path("config/freeradius/mods-enabled/sql").read_text(encoding="utf-8")
    query = re.search(rf'{name}\s*=\s*"(.*?)"', source, re.S).group(1).replace("\\", "")
    for variable, table in (
        ("authcheck_table", "radcheck"),
        ("authreply_table", "radreply"),
        ("usergroup_table", "radusergroup"),
    ):
        query = query.replace("${" + variable + "}", f'"{schema}"."{table}"')
    return query.replace("%{SQL-User-Name}", "test-login")


def test_actual_radius_queries_expire_without_worker_cleanup(engine):
    schema = f"test_connection_{uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        # RADIUS is an external projection, owned by this exact checked-in
        # PostgreSQL schema rather than app Base.metadata.
        conn.exec_driver_sql(
            Path("config/freeradius/schema.sql").read_text(encoding="utf-8"),
            execution_options={"no_parameters": True},
        )
        until = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
        conn.execute(
            text(
                "INSERT INTO radcheck (username, attribute, op, value) VALUES "
                "('test-login', 'Auth-Type', ':=', 'Reject'), "
                "('test-login', 'Dotmac-Test-Cleartext-Password', ':=', 'secret'), "
                "('test-login', 'Dotmac-Test-Until', ':=', :until)"
            ),
            {"until": str(until)},
        )
        conn.execute(
            text(
                "INSERT INTO radreply (username, attribute, op, value) VALUES "
                "('test-login', 'Mikrotik-Address-List', ':=', 'suspended'), "
                "('test-login', 'Dotmac-Test-Mikrotik-Rate-Limit', ':=', '100M/100M')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO radusergroup (username, groupname, priority) VALUES "
                "('test-login', 'dotmac-suspended', 0), ('test-login', 'Dotmac-Test-dotmac-active', 0)"
            )
        )
        checks = (
            conn.execute(text(_radius_query("authorize_check_query", schema=schema)))
            .mappings()
            .all()
        )
        replies = (
            conn.execute(text(_radius_query("authorize_reply_query", schema=schema)))
            .mappings()
            .all()
        )
        groups = (
            conn.execute(text(_radius_query("group_membership_query", schema=schema)))
            .scalars()
            .all()
        )
        assert {row["attribute"] for row in checks} == {
            "Cleartext-Password",
            "Tmp-Integer-0",
        }
        timeout = next(
            int(row["value"])
            for row in replies
            if row["attribute"] == "Session-Timeout"
        )
        assert 1 <= timeout <= 300
        assert "Mikrotik-Address-List" not in {row["attribute"] for row in replies}
        assert groups == ["dotmac-active"]
        # Leave all test rows intact, simulating a stopped timer/dispatcher.
        conn.execute(
            text(
                "UPDATE radcheck SET value = '1' WHERE attribute = 'Dotmac-Test-Until'"
            )
        )
        checks = (
            conn.execute(text(_radius_query("authorize_check_query", schema=schema)))
            .mappings()
            .all()
        )
        replies = (
            conn.execute(text(_radius_query("authorize_reply_query", schema=schema)))
            .mappings()
            .all()
        )
        groups = (
            conn.execute(text(_radius_query("group_membership_query", schema=schema)))
            .scalars()
            .all()
        )
        assert [(row["attribute"], row["value"]) for row in checks] == [
            ("Auth-Type", "Reject")
        ]
        assert [(row["attribute"], row["value"]) for row in replies] == [
            ("Mikrotik-Address-List", "suspended")
        ]
        assert groups == ["dotmac-suspended"]
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_predecessor_upgrade_adds_permission_without_replacing_role_grants(
    fresh_migration_database,
):
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    target = fresh_migration_database
    template_database.upgrade_to(target, "644_automation_scheduled_rules")
    migrated = create_engine(target)
    try:
        with migrated.begin() as conn:
            for name in (
                "customer_experience_manager",
                "finance_manager",
                "custom_support",
            ):
                conn.execute(
                    text(
                        "INSERT INTO roles (id, name, is_active) VALUES (:id, :name, true) ON CONFLICT (name) DO NOTHING"
                    ),
                    {"id": uuid4(), "name": name},
                )
            permission_id = uuid4()
            conn.execute(
                text(
                    "INSERT INTO permissions (id, key, is_active, is_ui_assignable, created_at, updated_at) VALUES (:id, 'test:existing_custom', true, true, now(), now())"
                ),
                {"id": permission_id},
            )
            conn.execute(
                text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) SELECT :id, id, :permission FROM roles WHERE name = 'custom_support'"
                ),
                {"id": uuid4(), "permission": permission_id},
            )
        command.upgrade(Config("alembic.ini"), "645_subscription_test_connection")
        with migrated.connect() as conn:
            granted = set(
                conn.execute(
                    text(
                        "SELECT roles.name FROM roles JOIN role_permissions ON role_permissions.role_id = roles.id JOIN permissions ON permissions.id = role_permissions.permission_id WHERE permissions.key = 'subscription:test_connection'"
                    )
                ).scalars()
            )
            assert {"customer_experience_manager", "finance_manager"} <= granted
            assert "custom_support" not in granted
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM role_permissions WHERE permission_id = :permission"
                    ),
                    {"permission": permission_id},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT to_regclass('public.test_connection_grants')")
                ).scalar_one()
                is not None
            )
    finally:
        migrated.dispose()


def test_concurrent_activations_serialize_to_one_grant(cloned_database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session

    from app.models.catalog import (
        AccessCredential,
        AccessType,
        CatalogOffer,
        PriceBasis,
        ServiceType,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.subscriber import Subscriber, SubscriberStatus
    from app.models.system_user import SystemUser
    from app.services.events import dispatcher
    from app.services.owner_commands import CommandContext
    from app.services.subscriber import _default_reseller_id

    target = cloned_database("645_subscription_test_connection")
    migrated = create_engine(target)
    try:
        with Session(migrated) as db:
            account = Subscriber(
                first_name="Concurrency",
                last_name="Test",
                email=f"{uuid4()}@example.com",
                status=SubscriberStatus.suspended,
                is_active=False,
                reseller_id=_default_reseller_id(db),
            )
            offer = CatalogOffer(
                name="Test plan",
                code=f"test-{uuid4()}",
                access_type=AccessType.fiber,
                service_type=ServiceType.residential,
                price_basis=PriceBasis.flat,
            )
            staff = SystemUser(
                first_name="Test",
                last_name="Engineer",
                email=f"{uuid4()}@example.com",
                is_active=True,
            )
            db.add_all([account, offer, staff])
            db.flush()
            sub = Subscription(
                subscriber_id=account.id,
                offer_id=offer.id,
                status=SubscriptionStatus.suspended,
                login=f"test-{uuid4()}",
            )
            db.add(sub)
            db.flush()
            db.add(
                AccessCredential(
                    subscriber_id=account.id,
                    subscription_id=sub.id,
                    username=sub.login,
                    secret_hash="fixture-secret",
                    is_active=False,
                )
            )
            account_id, sub_id, actor_id = account.id, sub.id, staff.id
            db.commit()
        monkeypatch.setattr(
            owner,
            "configuration",
            lambda db: owner.TestConnectionConfiguration(2, 24, True),
        )
        monkeypatch.setattr(
            "app.services.external_radius_targets.active_external_radius_targets",
            lambda db, **kwargs: [
                {"db_url": "postgresql://test@localhost/test_radius"}
            ],
        )
        monkeypatch.setattr(
            "app.services.credential_crypto.decrypt_credential",
            lambda value: "fixture-password",
        )
        monkeypatch.setattr(
            owner,
            "emit_event",
            lambda db, event_type, payload, **kwargs: dispatcher.emit_event(
                db, event_type, payload, dispatch_after_commit=False, **kwargs
            ),
        )
        barrier = Barrier(2)

        def activate():
            command_id = uuid4()
            context = CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=str(actor_id),
                scope=owner.PERMISSION,
                reason="Concurrency test",
            )
            barrier.wait(timeout=15)
            with Session(migrated) as db:
                try:
                    return owner.activate_test_connection(
                        db,
                        command=owner.ActivateTestConnectionCommand(
                            context, account_id, sub_id, actor_id
                        ),
                    ).grant_id
                except owner.TestConnectionError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(activate) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert results.count("access.test_connection.already_active") == 1
        with Session(migrated) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ConnectionGrant)
                    .where(ConnectionGrant.subscription_id == sub_id)
                )
                == 1
            )
    finally:
        migrated.dispose()
