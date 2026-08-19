"""Live canary for the shared foundation campaigns composes against.

Sub owns its database and migration lineage.  It therefore provides the four
named kernel effects installable tenant modules need, without composing the
kernel's colliding identity/RBAC/audit lineage.  The durable-timers module is
the first independently installed consumer and Sub selects only its tenant
plane.

This test deliberately reads PostgreSQL's catalogue rather than ORM metadata:
the security properties below exist only in the migrated database.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROVIDER_REVISION = "544_campaign_module_foundation"
TIMER_REVISION = "dt_0001_durable_timers"

HOST_TABLES = {
    "idempotency_records",
    "platform_idempotency_records",
    "outbox_events",
    "platform_outbox_events",
}
TENANT_TIMER_TABLES = {
    "timers",
    "timer_acceptances",
    "timer_rejections",
}
PLATFORM_TIMER_TABLES = {
    "platform_timers",
    "platform_timer_acceptances",
    "platform_timer_rejections",
}


def test_campaign_module_foundation_is_live_and_tenant_only(engine: Engine) -> None:
    assert engine.dialect.name == "postgresql"

    with engine.connect() as connection:
        revisions = set(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
        assert {PROVIDER_REVISION, TIMER_REVISION} <= revisions

        public_tables = set(
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public'"
                )
            ).scalars()
        )
        assert HOST_TABLES <= public_tables

        timer_tables = set(
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'mod_timers'"
                )
            ).scalars()
        )
        assert TENANT_TIMER_TABLES <= timer_tables
        assert PLATFORM_TIMER_TABLES.isdisjoint(timer_tables)

        roles = {
            str(row.rolname): (bool(row.rolbypassrls), bool(row.rolsuper))
            for row in connection.execute(
                text(
                    "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles "
                    "WHERE rolname IN "
                    "('app_admin', 'app_user', 'platform_api', "
                    "'outbox_dispatcher', 'platform_outbox_dispatcher')"
                )
            )
        }
        assert roles == {
            "app_admin": (True, False),
            "app_user": (False, False),
            "outbox_dispatcher": (False, False),
            "platform_api": (False, False),
            "platform_outbox_dispatcher": (False, False),
        }

        tenant_function = connection.scalar(
            text(
                "SELECT pg_get_functiondef("
                "to_regprocedure('public.app_current_tenant_id()'))"
            )
        )
        assert tenant_function is not None
        normalized = " ".join(str(tenant_function).lower().split())
        assert "current_setting('app.current_tenant'::text, true)" in normalized
        assert "invalid_text_representation" in normalized

        rls = {
            (str(row.nspname), str(row.relname)): (
                bool(row.relrowsecurity),
                bool(row.relforcerowsecurity),
            )
            for row in connection.execute(
                text(
                    "SELECT n.nspname, c.relname, c.relrowsecurity, "
                    "c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE (n.nspname = 'public' AND c.relname IN "
                    "('idempotency_records', 'platform_idempotency_records', "
                    "'outbox_events', 'platform_outbox_events')) "
                    "OR (n.nspname = 'mod_timers' AND c.relname IN "
                    "('timers', 'timer_acceptances', 'timer_rejections'))"
                )
            )
        }
        for table in (
            "idempotency_records",
            "outbox_events",
        ):
            assert rls[("public", table)] == (True, True)
        for table in (
            "platform_idempotency_records",
            "platform_outbox_events",
        ):
            assert rls[("public", table)] == (False, False)
        for table in TENANT_TIMER_TABLES:
            assert rls[("mod_timers", table)] == (True, True)

        relay_functions = {
            str(row.proname): (
                bool(row.prosecdef),
                str(row.owner),
                tuple(row.proconfig or ()),
            )
            for row in connection.execute(
                text(
                    "SELECT p.proname, p.prosecdef, "
                    "pg_get_userbyid(p.proowner) AS owner, p.proconfig "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname IN "
                    "('claim_outbox_batch', 'settle_outbox_event', "
                    "'claim_platform_outbox_batch', "
                    "'settle_platform_outbox_event')"
                )
            )
        }
        assert set(relay_functions) == {
            "claim_outbox_batch",
            "settle_outbox_event",
            "claim_platform_outbox_batch",
            "settle_platform_outbox_event",
        }
        for security_definer, owner, config in relay_functions.values():
            assert security_definer is True
            assert owner == "app_admin"
            assert "search_path=\"\"" in config

