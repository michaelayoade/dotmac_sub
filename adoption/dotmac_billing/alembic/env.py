"""Alembic environment for a disposable Billing tenant-shadow database."""

from __future__ import annotations

from logging.config import fileConfig

import dotmac_billing.models  # noqa: F401
import dotmac_kernel.models_platform  # noqa: F401
import dotmac_kernel.settings_models  # noqa: F401
from dotmac_kernel.messaging import models as messaging_models  # noqa: F401
from dotmac_kernel.models import Base
from dotmac_kernel.planes import install_module_plane_selections
from dotmac_kernel.prerequisites import install_prerequisite_bindings
from sqlalchemy import engine_from_config, pool

from alembic import context
from sub_billing_adoption.migration_bindings import (
    SHADOW_MODULE_PLANES,
    SHADOW_PREREQUISITE_BINDINGS,
)
from sub_billing_adoption.migrations import composed_version_locations

config = context.config
if not config.get_main_option("version_locations"):
    config.set_main_option("version_locations", composed_version_locations())
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

install_prerequisite_bindings(SHADOW_PREREQUISITE_BINDINGS)
install_module_plane_selections(SHADOW_MODULE_PLANES)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
