"""Fail a deployment when required database structures are not usable.

Run after ``alembic upgrade heads`` and before replacing live services.  The
check is read-only and deliberately examines PostgreSQL catalog validity, not
only object names.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import SessionLocal
from app.services.integrations.registry import connector_definition
from scripts.migration.radius_session_latest_index import (
    invalid_postgres_indexes,
    validate_postgres_index,
)


@dataclass(frozen=True, slots=True)
class InvalidManifestPin:
    installation_name: str
    connector_key: str
    installed_version: str
    deployed_version: str | None
    installed_digest: str
    deployed_digest: str | None


def invalid_enabled_manifest_pins(
    bind: Connection,
) -> tuple[InvalidManifestPin, ...]:
    """Return enabled installations that cannot execute in the candidate image."""

    rows = bind.execute(
        text(
            """
            SELECT name, connector_key, connector_version, manifest_digest
            FROM integration_installations
            WHERE state = 'enabled'
            ORDER BY connector_key, name
            """
        )
    ).mappings()
    invalid: list[InvalidManifestPin] = []
    for row in rows:
        definition = connector_definition(str(row["connector_key"]))
        if definition is not None and (
            str(row["connector_version"]) == definition.version
            and str(row["manifest_digest"]) == definition.digest
        ):
            continue
        invalid.append(
            InvalidManifestPin(
                installation_name=str(row["name"]),
                connector_key=str(row["connector_key"]),
                installed_version=str(row["connector_version"]),
                deployed_version=definition.version if definition else None,
                installed_digest=str(row["manifest_digest"]),
                deployed_digest=definition.digest if definition else None,
            )
        )
    return tuple(invalid)


def verify_schema_contracts(bind: Connection) -> None:
    if bind.dialect.name != "postgresql":
        return

    validate_postgres_index(bind)
    invalid = invalid_postgres_indexes(bind)
    if invalid:
        rendered = ", ".join(
            f"{schema}.{index} on {table}" for schema, table, index in invalid
        )
        raise RuntimeError(
            "database contains invalid or unready indexes after migration: " + rendered
        )
    invalid_pins = invalid_enabled_manifest_pins(bind)
    if invalid_pins:
        rendered = ", ".join(
            (
                f"{item.installation_name} ({item.connector_key}) "
                f"installed={item.installed_version}/"
                f"{item.installed_digest[:12]} "
                f"deployed={item.deployed_version or 'missing'}/"
                f"{(item.deployed_digest or 'missing')[:12]}"
            )
            for item in invalid_pins
        )
        raise RuntimeError(
            "enabled integration manifest pins do not match the candidate "
            "deployment; add an explicit adoption migration before replacing "
            "services: " + rendered
        )


def main() -> None:
    db = SessionLocal()
    try:
        verify_schema_contracts(db.connection())
    finally:
        db.close()
    print("Database schema contracts verified.")


if __name__ == "__main__":
    main()
