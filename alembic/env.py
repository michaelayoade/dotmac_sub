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
    _original_drop_table = op.drop_table
    _original_create_index = op.create_index
    _original_drop_index = op.drop_index
    _original_create_unique_constraint = op.create_unique_constraint
    _original_create_check_constraint = op.create_check_constraint
    _original_create_foreign_key = op.create_foreign_key

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

    def _index_exists(table_name: str, index_name: str) -> bool:
        try:
            inspector = sa.inspect(op.get_bind())
            return any(
                ix["name"] == index_name for ix in inspector.get_indexes(table_name)
            )
        except Exception:
            return False

    def _constraint_exists(table_name: str, constraint_name: str) -> bool:
        try:
            inspector = sa.inspect(op.get_bind())
            unique = {c["name"] for c in inspector.get_unique_constraints(table_name)}
            checks = {c["name"] for c in inspector.get_check_constraints(table_name)}
            fks = {fk["name"] for fk in inspector.get_foreign_keys(table_name)}
            return constraint_name in (unique | checks | fks)
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

    def _safe_drop_table(table_name, *args, **kwargs):
        if not _table_exists(table_name):
            return None
        return _original_drop_table(table_name, *args, **kwargs)

    def _safe_create_index(index_name, table_name, *args, **kwargs):
        if _index_exists(table_name, index_name):
            return None
        return _original_create_index(index_name, table_name, *args, **kwargs)

    def _safe_drop_index(index_name, table_name=None, *args, **kwargs):
        if table_name and not _index_exists(table_name, index_name):
            return None
        return _original_drop_index(index_name, table_name, *args, **kwargs)

    def _safe_create_unique_constraint(constraint_name, table_name, *args, **kwargs):
        if _constraint_exists(table_name, constraint_name):
            return None
        return _original_create_unique_constraint(
            constraint_name, table_name, *args, **kwargs
        )

    def _safe_create_check_constraint(constraint_name, table_name, *args, **kwargs):
        if _constraint_exists(table_name, constraint_name):
            return None
        return _original_create_check_constraint(
            constraint_name, table_name, *args, **kwargs
        )

    def _safe_create_foreign_key(constraint_name, source_table, *args, **kwargs):
        if constraint_name and _constraint_exists(source_table, constraint_name):
            return None
        return _original_create_foreign_key(
            constraint_name, source_table, *args, **kwargs
        )

    op.add_column = _safe_add_column
    op.drop_column = _safe_drop_column
    op.create_table = _safe_create_table
    op.drop_table = _safe_drop_table
    op.create_index = _safe_create_index
    op.drop_index = _safe_drop_index
    op.create_unique_constraint = _safe_create_unique_constraint
    op.create_check_constraint = _safe_create_check_constraint
    op.create_foreign_key = _safe_create_foreign_key


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
