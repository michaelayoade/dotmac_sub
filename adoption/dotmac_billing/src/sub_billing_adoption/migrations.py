"""Programmatic Alembic config for the isolated kernel+Billing graph."""

from __future__ import annotations

import os
from pathlib import Path

from dotmac_billing import versions_dir as billing_versions_dir
from dotmac_kernel.migrations import versions_dir as kernel_versions_dir
from dotmac_kernel.planes import MODULE_PLANES_ENV_VAR
from dotmac_kernel.prerequisites import BINDINGS_ENV_VAR

from alembic.config import Config

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = PACKAGE_ROOT / "alembic"


def composed_version_locations() -> str:
    return f"{kernel_versions_dir()} {billing_versions_dir()}"


def make_shadow_alembic_config(database_url: str) -> Config:
    """Build the only graph the isolated rehearsal is allowed to migrate."""

    config = Config(str(PACKAGE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("version_locations", composed_version_locations())
    config.set_main_option("sqlalchemy.url", database_url)
    os.environ[BINDINGS_ENV_VAR] = (
        "sub_billing_adoption.migration_bindings:SHADOW_PREREQUISITE_BINDINGS"
    )
    os.environ[MODULE_PLANES_ENV_VAR] = (
        "sub_billing_adoption.migration_bindings:SHADOW_MODULE_PLANES"
    )
    return config


__all__ = [
    "ALEMBIC_DIR",
    "PACKAGE_ROOT",
    "composed_version_locations",
    "make_shadow_alembic_config",
]
