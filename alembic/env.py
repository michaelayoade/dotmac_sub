from logging.config import fileConfig

from sqlalchemy import Column, MetaData, String, Table, engine_from_config, pool, text

from alembic import context
from app.config import settings
from app.db import Base
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
    webhook,
    wireguard,
)

config = context.config

config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def ensure_alembic_version_table(connection) -> None:
    """Use a wider revision column for this repo's descriptive revision IDs."""
    version_table = Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(255), primary_key=True),
    )
    version_table.create(connection, checkfirst=True)

    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"
            )
        )


def include_object(object, name, type_, reflected, compare_to):
    """Exclude PostGIS system tables from autogenerate."""
    if type_ == "table" and name in ("spatial_ref_sys",):
        return False
    return True


def _install_idempotent_schema_ops() -> None:
    """Wrap alembic schema ops so they tolerate already-present state.

    The squashed initial migration (001) builds the full current schema
    via ``Base.metadata.create_all``. Subsequent migrations were written
    against the pre-squash incremental schema and unconditionally
    ``op.add_column`` / ``op.drop_column`` / ``op.create_table`` columns
    and tables that the squash already produced. Without this wrapper a
    fresh squash-built DB explodes with DuplicateColumn / DuplicateTable
    /  UndefinedColumn errors during the migration chain.

    Each wrapped op checks the live schema via ``sa.inspect(op.get_bind())``
    and no-ops when the target is already in the desired state. Pre-existing
    production DBs (where the schema is the pre-squash incremental state)
    see exactly the same behavior as before.
    """
    import sqlalchemy as sa  # noqa: PLC0415 — alembic env is import-time

    from alembic import op  # noqa: PLC0415

    _original_add_column = op.add_column
    _original_drop_column = op.drop_column
    _original_create_table = op.create_table

    def _columns_of(table_name: str) -> set[str]:
        try:
            inspector = sa.inspect(op.get_bind())
            return {c["name"] for c in inspector.get_columns(table_name)}
        except Exception:
            return set()

    def _table_exists(table_name: str) -> bool:
        try:
            inspector = sa.inspect(op.get_bind())
            return table_name in inspector.get_table_names()
        except Exception:
            return False

    def _safe_add_column(table_name, column, *args, **kwargs):
        if column.name in _columns_of(table_name):
            return None
        return _original_add_column(table_name, column, *args, **kwargs)

    def _safe_drop_column(table_name, column_name, *args, **kwargs):
        if column_name not in _columns_of(table_name):
            return None
        return _original_drop_column(table_name, column_name, *args, **kwargs)

    def _safe_create_table(table_name, *args, **kwargs):
        if _table_exists(table_name):
            return None
        return _original_create_table(table_name, *args, **kwargs)

    op.add_column = _safe_add_column
    op.drop_column = _safe_drop_column
    op.create_table = _safe_create_table


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


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        ensure_alembic_version_table(connection)
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        _install_idempotent_schema_ops()

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
