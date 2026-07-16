"""Read-only gate for collapsing RADIUS projection onto one physical writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import Column, String, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RadiusDatabaseIdentity:
    host: str
    port: int | None
    database: str

    @property
    def label(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.host}{port}/{self.database}"


@dataclass(frozen=True)
class RadiusWriterTargetAssessment:
    source: str
    target: str
    matches_canonical: bool
    schema_contract_ok: bool
    use_group: bool
    group_rows_present: bool
    error: str | None = None


@dataclass(frozen=True)
class RadiusWriterEquivalenceReport:
    canonical_target: str | None
    target_count: int
    unique_target_count: int
    all_targets_match_canonical: bool
    schema_contract_ok: bool
    group_semantics_required: bool
    ready_for_single_owner: bool
    targets: tuple[RadiusWriterTargetAssessment, ...]

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "targets": [asdict(target) for target in self.targets],
        }


def database_identity(dsn: str | None) -> RadiusDatabaseIdentity | None:
    if not dsn:
        return None
    try:
        url = make_url(dsn)
    except Exception:
        return None
    if url.get_backend_name() == "sqlite":
        return RadiusDatabaseIdentity(
            host="sqlite",
            port=None,
            database=str(url.database or ":memory:"),
        )
    return RadiusDatabaseIdentity(
        host=str(url.host or "").strip().lower(),
        port=url.port or 5432,
        database=str(url.database or "").strip(),
    )


def _table_parts(identifier: str) -> tuple[str | None, str]:
    parts = identifier.split(".", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def _probe_schema(config: dict) -> tuple[bool, bool, str | None]:
    from app.services.radius import _get_external_engine

    required = {
        config["radcheck_table"]: {"username", "attribute", "op", "value"},
        config["radreply_table"]: {"username", "attribute", "op", "value"},
        config["radusergroup_table"]: {"username", "groupname", "priority"},
    }
    try:
        engine = _get_external_engine(config["db_url"])
        with engine.connect() as conn:
            inspector = inspect(conn)
            for identifier, expected_columns in required.items():
                schema, table = _table_parts(identifier)
                columns = {
                    column["name"]
                    for column in inspector.get_columns(table, schema=schema)
                }
                missing = expected_columns - columns
                if missing:
                    return (
                        False,
                        False,
                        f"{identifier} missing columns: {', '.join(sorted(missing))}",
                    )
            from app.services.radius import _external_radius_table

            group_table = _external_radius_table(
                config["radusergroup_table"], Column("username", String)
            )
            group_rows_present = (
                conn.execute(select(group_table.c.username).limit(1)).first()
                is not None
            )
        return True, group_rows_present, None
    except Exception as exc:
        return False, False, f"{type(exc).__name__}: {exc}"


def assess_radius_writer_equivalence(
    db: Session,
    *,
    probe_schema: bool = True,
) -> RadiusWriterEquivalenceReport:
    from app.services import radius as radius_service
    from app.services import radius_dsn

    canonical = database_identity(radius_dsn.resolve_radius_dsn())
    configs = radius_service._active_external_sync_configs(db)
    assessments: list[RadiusWriterTargetAssessment] = []
    identities: set[RadiusDatabaseIdentity] = set()

    for index, config in enumerate(configs, start=1):
        identity = database_identity(str(config.get("db_url") or ""))
        if identity is not None:
            identities.add(identity)
        schema_ok = not probe_schema
        group_rows_present = False
        error = None
        if probe_schema:
            schema_ok, group_rows_present, error = _probe_schema(config)
        assessments.append(
            RadiusWriterTargetAssessment(
                source=f"external_sync_{index}",
                target=identity.label if identity is not None else "unresolved",
                matches_canonical=(
                    canonical is not None
                    and identity is not None
                    and identity == canonical
                ),
                schema_contract_ok=schema_ok,
                use_group=bool(config.get("use_group")),
                group_rows_present=group_rows_present,
                error=error,
            )
        )

    all_match = bool(assessments) and all(
        target.matches_canonical for target in assessments
    )
    schema_ok = bool(assessments) and all(
        target.schema_contract_ok for target in assessments
    )
    groups_required = any(
        target.use_group or target.group_rows_present for target in assessments
    )
    return RadiusWriterEquivalenceReport(
        canonical_target=canonical.label if canonical is not None else None,
        target_count=len(assessments),
        unique_target_count=len(identities),
        all_targets_match_canonical=all_match,
        schema_contract_ok=schema_ok,
        group_semantics_required=groups_required,
        ready_for_single_owner=(all_match and schema_ok and not groups_required),
        targets=tuple(assessments),
    )
