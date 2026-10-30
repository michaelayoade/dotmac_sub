import os
from logging.config import fileConfig
from pathlib import Path

from dotmac_kernel.planes import install_module_plane_selections
from dotmac_kernel.prerequisites import install_prerequisite_bindings
from sqlalchemy import Column, MetaData, String, Table, engine_from_config, pool, text

from alembic import context
from app.config import settings
from app.db import (
    Base,
    apply_migration_idle_transaction_timeout,
    resolve_migration_lock_timeout,
)
from app.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)
from app.migration_ownership import (
    assert_migrator_identity,
    assert_owner_variable_set_for_bypass,
    grant_runtime_privileges,
    load_definer_allowlist,
    runtime_role_from_environ,
)
from app.migration_schema_ops import install_idempotent_schema_ops
from app.models import (  # noqa: F401
    analytics,
    audit,
    auth,
    bandwidth,
    billing,
    catalog,
    collections,
    comms,
    connector,
    contracts,
    domain_settings,
    event_store,
    external,
    fiber_change_request,
    gis,
    integration,
    legal,
    lifecycle,
    network,
    network_monitoring,
    notification,
    oauth_token,
    payment_arrangement,
    provisioning,
    qualification,
    radius,
    rbac,
    scheduler,
    sequence,
    snmp,
    stored_file,
    subscriber,
    subscription_change,
    subscription_engine,
    table_column_config,
    table_column_default_config,
    tr069,
    usage,
    wireguard,
)

config = context.config

# ConfigParser interpolates percent signs; escape only at this write boundary.
# Both offline SQL generation and the online engine read the original URL back.
config.set_main_option(
    "sqlalchemy.url", settings.alembic_database_url.replace("%", "%%")
)

# Installed BEFORE the revision map is built. A composed module's migration
# resolves its `depends_on` from these bindings at script-load time, so an
# assembly that composes a module without answering what it requires fails
# loudly here rather than ordering wrongly. See `app/migration_bindings.py`.
install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
install_module_plane_selections(ASSEMBLY_MODULE_PLANES)

# Composed module lineages are declared as installed `package:resource`
# locations in alembic.ini. Alembic builds its ScriptDirectory before this file
# runs, so changing `version_locations` here would be a silent no-op for the
# current command. The architecture gate keeps those locations, dependency
# pins, prerequisite bindings and plane selections aligned.

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# VAS was retired in revision 291. These tables remain as immutable financial
# history and must not be proposed for deletion merely because their active ORM
# models were removed from the application.
RETIRED_ARCHIVE_TABLES = frozenset(
    {
        "payment_prepaid_applications_archive",
        "vas_wallets",
        "vas_wallet_entries",
        "vas_refund_requests",
        "vas_services",
        "vas_service_variations",
        "vas_transactions",
        "vas_rate_cards",
        "vas_topup_intents",
    }
)


DEFINER_ALLOWLIST_PATH = Path(__file__).with_name("definer_owner_allowlist.txt")

#: `varchar(255)` is stored as atttypmod 255 + VARHDRSZ (4).
_VERSION_NUM_TYPMOD = 259


def ensure_alembic_version_table(connection) -> None:
    """Use a wider revision column for this repo's descriptive revision IDs."""
    version_table = Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(255), primary_key=True),
    )
    version_table.create(connection, checkfirst=True)

    if connection.dialect.name == "postgresql":
        # Conditional: an unconditional ALTER needs table ownership on every
        # run and takes a lock even when there is nothing to widen.
        typmod = connection.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = to_regclass('alembic_version') "
                "AND attname = 'version_num' AND NOT attisdropped"
            )
        ).scalar()
        if typmod != _VERSION_NUM_TYPMOD:
            connection.execute(
                text(
                    "ALTER TABLE alembic_version "
                    "ALTER COLUMN version_num TYPE VARCHAR(255)"
                )
            )


def include_object(object, name, type_, reflected, compare_to):
    """Exclude system and intentionally archived tables from autogenerate."""
    if type_ == "table" and name in {"spatial_ref_sys", *RETIRED_ARCHIVE_TABLES}:
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _set_migration_lock_timeout(connection) -> None:
    """Bound how long a migration waits to ACQUIRE a lock, so a schema-locking
    statement (e.g. an ``ADD COLUMN`` needing ACCESS EXCLUSIVE) fails fast
    instead of queuing behind the live app's locks and piling every subsequent
    query behind it (the seabone/prod lock-trap that turned a cheap DDL into a
    20-minute stall). Bounds lock *acquisition* only, NOT statement runtime, so
    long data migrations are unaffected. Postgres only — SQLite (the test DB)
    has no lock_timeout. Override via ``ALEMBIC_LOCK_TIMEOUT`` (e.g. ``30s`` for
    a maintenance window, ``0`` to disable).
    """
    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(
        f"SET lock_timeout = '{resolve_migration_lock_timeout()}'"
    )


def _set_migration_idle_transaction_timeout(connection) -> None:
    """Bound how long this connection may sit idle-in-transaction with no
    query activity, so Postgres's ``idle_in_transaction_session_timeout``
    cannot kill it while ``context.run_migrations()`` loads, imports and
    topologically sorts every file under ``alembic/versions/`` (632+ and
    growing) BEFORE the first real migration statement runs — pure Python
    work with zero DB activity (the 2026-09-09 failed-deploy incident: the
    connection was terminated on the very first schema-reflection query of
    the first pending migration, before any real migration ran; the deploy's
    own rollback fired correctly and production was never touched, but the
    release couldn't complete). Bounds idle time between statements, NOT
    total transaction duration, so a long-running data migration that is
    actually executing statements is unaffected. Postgres only — SQLite (the
    test DB) has no such setting. Override via
    ``ALEMBIC_IDLE_TRANSACTION_TIMEOUT`` (e.g. ``0`` to disable). See
    ``app.db.apply_migration_idle_transaction_timeout`` for the actual
    ``SET`` and why it is session-level rather than ``SET LOCAL`` here.
    """
    apply_migration_idle_transaction_timeout(connection)


def _migration_runtime_role(connection) -> str | None:
    """The runtime role granted access to every migration-created object, or ``None``.

    Set by the deploy (``MIGRATION_RUNTIME_ROLE=dotmac_app``) when migrations
    run as the BYPASSRLS migrator. New objects stay owned by the migrator; the
    runtime role gets explicit DML grants in the same transaction. Unset keeps
    the historical behaviour, so local and CI runs as a single owner or
    superuser are unchanged. The legacy ``MIGRATION_OBJECT_OWNER`` spelling is
    still read (see ``runtime_role_from_environ``).
    """
    role = runtime_role_from_environ(os.environ)
    if not role or connection.dialect.name != "postgresql":
        return None
    return role


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Timeouts first: nothing below may wait on a lock unbounded.
        _set_migration_lock_timeout(connection)
        _set_migration_idle_transaction_timeout(connection)

        owner = _migration_runtime_role(connection)
        if owner is None and connection.dialect.name == "postgresql":
            assert_owner_variable_set_for_bypass(connection)
        allowlist = frozenset()
        if owner is not None:
            assert_migrator_identity(connection, owner=owner)
            allowlist = load_definer_allowlist(DEFINER_ALLOWLIST_PATH)

        ensure_alembic_version_table(connection)
        if owner is not None:
            # A fresh database creates alembic_version here, committed below,
            # so the runtime grant must land before that commit. The runtime
            # reads the revision; only the migrator writes it.
            grant_runtime_privileges(
                connection,
                runtime_role=owner,
                definer_allowlist=allowlist,
            )
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        install_idempotent_schema_ops()

        with context.begin_transaction():
            context.run_migrations()
            if owner is not None:
                grant_runtime_privileges(
                    connection,
                    runtime_role=owner,
                    definer_allowlist=allowlist,
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
