"""Programmatic Alembic composition for Sub-owned and installed lineages.

Installed package paths vary between a Poetry environment and a container, so
``alembic.ini`` cannot name them safely.  Every migration entry point builds a
Config here before Alembic constructs its revision map.  The kernel core
lineage is deliberately absent; Sub provides the small shared effects modules
need from its own application lineage.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from dotmac_durable_timers.manifest import module as durable_timers_module
from dotmac_durable_timers.migrations import versions_dir as timer_versions_dir
from dotmac_kernel.planes import ModulePlane, ModulePlaneSelection
from dotmac_kernel.prerequisites import BINDINGS_ENV_VAR

from app.migration_bindings import BINDINGS_REFERENCE

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
ALEMBIC_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "alembic"
SUB_VERSIONS: Final[Path] = ALEMBIC_DIRECTORY / "versions"

MODULE_PLANE_SELECTIONS: Final[tuple[ModulePlaneSelection, ...]] = (
    ModulePlaneSelection(
        module=durable_timers_module.code,
        planes=(ModulePlane.TENANT,),
    ),
)


def version_locations() -> tuple[Path, ...]:
    """Every composed lineage; never the kernel core lineage."""

    locations = (SUB_VERSIONS, Path(timer_versions_dir()).resolve())
    for location in locations:
        if not location.is_dir():
            raise RuntimeError(
                f"composed migration lineage does not exist: {location}"
            )
        if " " in str(location):
            raise RuntimeError(
                "a migration lineage path contains a space and cannot be "
                f"represented by Alembic's space separator: {location}"
            )
    return locations


def version_locations_setting() -> str:
    return " ".join(str(location) for location in version_locations())


def make_alembic_config(database_url: str | None = None) -> Config:
    """Build the one Config used by deploys, tests and graph inspection."""

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_DIRECTORY))
    config.set_main_option("version_locations", version_locations_setting())
    config.attributes["module_plane_selections"] = MODULE_PLANE_SELECTIONS
    os.environ[BINDINGS_ENV_VAR] = BINDINGS_REFERENCE

    resolved_url = (
        database_url
        or os.getenv("MIGRATION_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )
    if resolved_url:
        config.set_main_option("sqlalchemy.url", resolved_url)
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrations")
    parser.add_argument(
        "action", choices=("upgrade", "downgrade", "current", "heads", "history")
    )
    parser.add_argument("revision", nargs="?", default="heads")
    args = parser.parse_args(argv)

    config = make_alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    elif args.action == "downgrade":
        command.downgrade(config, args.revision)
    elif args.action == "current":
        command.current(config, verbose=True)
    elif args.action == "heads":
        command.heads(config, verbose=True)
    else:
        command.history(config, verbose=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ALEMBIC_DIRECTORY",
    "MODULE_PLANE_SELECTIONS",
    "REPOSITORY_ROOT",
    "SUB_VERSIONS",
    "make_alembic_config",
    "version_locations",
    "version_locations_setting",
]
