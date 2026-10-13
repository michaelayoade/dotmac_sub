"""Candidate-graph and catalogue owner for early secret feature coverage.

Only PostgreSQL observations from the caller's connection are authoritative.
This owner never treats a failed observation as an empty feature population.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session

from scripts.ci.migrated_test_database import install_migration_graph_environment
from scripts.release_artifact_contract import GitCommitSha, ProductionServerName
from scripts.release_candidate_evidence import (
    read_bootstrap_authorization,
    verify_bootstrap_authorization,
)


class SchemaCapabilityState(StrEnum):
    EVALUATED = "evaluated"
    NOT_INSTALLED_AT_PREDECESSOR = "not_installed_at_predecessor"
    AUTHORIZED_EMPTY_BOOTSTRAP = "authorized_empty_bootstrap"
    EVALUATION_UNAVAILABLE = "evaluation_unavailable"


@dataclass(frozen=True, slots=True)
class DeploymentSecretSchemaQuery:
    bootstrap_authorization_path: Path | None = field(default=None, repr=False)
    candidate_revision: GitCommitSha | None = None
    target_server: ProductionServerName | None = None


@dataclass(frozen=True, slots=True)
class DeploymentSecretSchemaResult:
    integrations: SchemaCapabilityState
    machine_credentials: SchemaCapabilityState
    machine_attribution_available: bool
    authoritative: bool = True

    @property
    def ok(self) -> bool:
        return SchemaCapabilityState.EVALUATION_UNAVAILABLE not in (
            self.integrations,
            self.machine_credentials,
        )


_INTEGRATION_ANCHOR = "376_integration_platform_foundation"
_MACHINE_ANCHOR = "551_machine_credentials"
_ATTRIBUTION_ANCHOR = "639_machine_attribution"
_INTEGRATION_COLUMNS = {
    "integration_installations": frozenset(
        {
            "id",
            "connector_key",
            "connector_version",
            "manifest_digest",
            "state",
            "current_config_revision_id",
        }
    ),
    "integration_config_revisions": frozenset({"id", "installation_id", "secret_refs"}),
}
_MACHINE_COLUMNS = {
    "machine_credentials": frozenset(
        {"tenant_id", "is_active", "revoked_at", "expires_at"}
    )
}


@dataclass(frozen=True, slots=True)
class _Catalogue:
    columns: dict[str, frozenset[str]]
    application_relations: frozenset[tuple[str, str]]
    application_schemas: frozenset[str]
    application_objects_present: bool = False

    @property
    def empty(self) -> bool:
        return (
            not self.application_relations
            and not self.application_schemas
            and not self.application_objects_present
        )


def _unavailable(*, authoritative: bool = True) -> DeploymentSecretSchemaResult:
    return DeploymentSecretSchemaResult(
        SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        SchemaCapabilityState.EVALUATION_UNAVAILABLE,
        False,
        authoritative=authoritative,
    )


def _candidate_graph() -> ScriptDirectory:
    install_migration_graph_environment()
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    graph = ScriptDirectory.from_config(config)
    graph.get_heads()  # Force candidate graph validation even for empty bootstrap.
    for anchor in (_INTEGRATION_ANCHOR, _MACHINE_ANCHOR, _ATTRIBUTION_ANCHOR):
        if graph.get_revision(anchor) is None:
            raise ValueError("candidate capability anchor unavailable")
    return graph


def _ancestors(graph: ScriptDirectory, revisions: frozenset[str]) -> frozenset[str]:
    # Alembic traverses real lineage and depends_on edges, including composed
    # module planes. Revision prefixes are never treated as an ordering.
    return frozenset(
        item.revision for item in graph.iterate_revisions(tuple(revisions), "base")
    )


def _feature_state(
    *,
    graph: ScriptDirectory,
    current: frozenset[str],
    applied: frozenset[str],
    anchor: str,
    expected: dict[str, frozenset[str]],
    catalogue: _Catalogue,
) -> SchemaCapabilityState:
    if anchor in applied:
        return (
            SchemaCapabilityState.EVALUATED
            if all(
                columns <= catalogue.columns.get(table, frozenset())
                for table, columns in expected.items()
            )
            else SchemaCapabilityState.EVALUATION_UNAVAILABLE
        )
    predecessors = _ancestors(graph, frozenset({anchor})) - {anchor}
    if current <= predecessors and not any(
        table in catalogue.columns
        or ("public", table) in catalogue.application_relations
        for table in expected
    ):
        return SchemaCapabilityState.NOT_INSTALLED_AT_PREDECESSOR
    return SchemaCapabilityState.EVALUATION_UNAVAILABLE


def _classify_versioned(
    graph: ScriptDirectory,
    current: frozenset[str],
    catalogue: _Catalogue,
) -> DeploymentSecretSchemaResult:
    if not current:
        return _unavailable()
    for revision in current:
        resolved = graph.get_revision(revision)
        if resolved is None or resolved.revision != revision:
            return _unavailable()
    # An Alembic version set is a frontier, never an ancestor plus descendant.
    if any(
        current & (_ancestors(graph, frozenset({head})) - {head}) for head in current
    ):
        return _unavailable()
    applied = _ancestors(graph, current)
    integration = _feature_state(
        graph=graph,
        current=current,
        applied=applied,
        anchor=_INTEGRATION_ANCHOR,
        expected=_INTEGRATION_COLUMNS,
        catalogue=catalogue,
    )
    machine = _feature_state(
        graph=graph,
        current=current,
        applied=applied,
        anchor=_MACHINE_ANCHOR,
        expected=_MACHINE_COLUMNS,
        catalogue=catalogue,
    )
    attribution = "source_application" in catalogue.columns.get(
        "machine_credentials", frozenset()
    )
    if _ATTRIBUTION_ANCHOR in applied:
        if machine is not SchemaCapabilityState.EVALUATED or not attribution:
            machine = SchemaCapabilityState.EVALUATION_UNAVAILABLE
    elif machine is SchemaCapabilityState.EVALUATED:
        if (
            not current
            <= (
                _ancestors(graph, frozenset({_ATTRIBUTION_ANCHOR}))
                - {_ATTRIBUTION_ANCHOR}
            )
            or attribution
        ):
            machine = SchemaCapabilityState.EVALUATION_UNAVAILABLE
    return DeploymentSecretSchemaResult(
        integration, machine, attribution and machine is SchemaCapabilityState.EVALUATED
    )


_RELATIONS_SQL = """
SELECT n.nspname, c.relname, c.relkind
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
  AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend d
    WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass AND d.objid = c.oid
      AND d.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND d.deptype = 'e'
  )
"""
_SCHEMAS_SQL = """
SELECT n.nspname FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
  AND n.nspname NOT IN ('information_schema', 'public')
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend d
    WHERE d.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass AND d.objid = n.oid
      AND d.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND d.deptype = 'e'
  )
"""
_NON_RELATIONAL_OBJECTS_SQL = """
WITH RECURSIVE extension_owned(classid, objid) AS (
    SELECT classid, objid FROM pg_catalog.pg_depend
    WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND deptype = 'e'
    UNION
    SELECT d.classid, d.objid FROM pg_catalog.pg_depend d
    JOIN extension_owned e ON d.refclassid = e.classid AND d.refobjid = e.objid
    WHERE d.deptype IN ('i', 'a')
), version_owned_types(oid) AS (
    SELECT t.oid
    FROM pg_catalog.pg_type t
    JOIN pg_catalog.pg_class c ON c.oid = t.typrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
      AND c.relkind IN ('r', 'p')
    UNION
    SELECT child.oid
    FROM version_owned_types parent
    JOIN pg_catalog.pg_type owned ON owned.oid = parent.oid
    JOIN pg_catalog.pg_type child ON child.oid = owned.typarray
                                AND child.typelem = owned.oid
    JOIN pg_catalog.pg_depend d ON d.objid = child.oid
                               AND d.refobjid = owned.oid
    WHERE d.classid = 'pg_catalog.pg_type'::pg_catalog.regclass
      AND d.refclassid = 'pg_catalog.pg_type'::pg_catalog.regclass
      AND d.deptype IN ('i', 'a')
), application_objects AS (
    SELECT p.pronamespace AS namespace, 'pg_catalog.pg_proc'::pg_catalog.regclass AS classid,
           p.oid AS objid
    FROM pg_catalog.pg_proc p
    UNION ALL
    SELECT t.typnamespace, 'pg_catalog.pg_type'::pg_catalog.regclass, t.oid
    FROM pg_catalog.pg_type t
    WHERE NOT EXISTS (SELECT 1 FROM version_owned_types v WHERE v.oid = t.oid)
    UNION ALL
    SELECT oprnamespace, 'pg_catalog.pg_operator'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_operator
    UNION ALL
    SELECT opcnamespace, 'pg_catalog.pg_opclass'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_opclass
    UNION ALL
    SELECT opfnamespace, 'pg_catalog.pg_opfamily'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_opfamily
    UNION ALL
    SELECT collnamespace, 'pg_catalog.pg_collation'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_collation
    UNION ALL
    SELECT connamespace, 'pg_catalog.pg_conversion'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_conversion
    UNION ALL
    SELECT cfgnamespace, 'pg_catalog.pg_ts_config'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_ts_config
    UNION ALL
    SELECT dictnamespace, 'pg_catalog.pg_ts_dict'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_ts_dict
    UNION ALL
    SELECT prsnamespace, 'pg_catalog.pg_ts_parser'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_ts_parser
    UNION ALL
    SELECT tmplnamespace, 'pg_catalog.pg_ts_template'::pg_catalog.regclass, oid
    FROM pg_catalog.pg_ts_template
)
SELECT EXISTS (
    SELECT 1 FROM application_objects o
    JOIN pg_catalog.pg_namespace n ON n.oid = o.namespace
    WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
      AND n.nspname <> 'information_schema'
      AND NOT EXISTS (
        SELECT 1 FROM extension_owned e
        WHERE e.classid = o.classid AND e.objid = o.objid
      )
)
"""

# Compare actual unqualified runtime resolution (including pg_temp) to the
# public relation OID observed in this same catalogue snapshot. Checking absent
# public names too prevents a shadow table from masquerading as a predecessor
# or fresh bootstrap. These are the complete table inputs of the feature query.
_REQUIREMENT_RELATIONS = frozenset(
    {
        "machine_credentials",
        "integration_installations",
        "integration_config_revisions",
        "alembic_version",
    }
)
_RELATION_RESOLUTION_SQL = """
WITH names(relname) AS (
    VALUES ('machine_credentials'), ('integration_installations'),
           ('integration_config_revisions'), ('alembic_version')
)
SELECT names.relname, c.oid,
       pg_catalog.to_regclass(pg_catalog.quote_ident(names.relname))::pg_catalog.oid
FROM names
LEFT JOIN pg_catalog.pg_namespace n ON n.nspname = 'public'
LEFT JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid
                              AND c.relname = names.relname
"""

_COLUMNS_SQL = """
SELECT c.relname, a.attname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  AND a.attnum > 0 AND NOT a.attisdropped
"""


def _bootstrap_verified(query: DeploymentSecretSchemaQuery) -> bool:
    if (
        query.bootstrap_authorization_path is None
        or query.candidate_revision is None
        or query.target_server is None
    ):
        return False
    authorization = read_bootstrap_authorization(query.bootstrap_authorization_path)
    verify_bootstrap_authorization(
        authorization,
        target_revision=query.candidate_revision,
        target_server=query.target_server,
    )
    return True


def evaluate_deployment_secret_schema(
    db: Session,
    *,
    query: DeploymentSecretSchemaQuery,
) -> DeploymentSecretSchemaResult:
    """Read exact candidate graph/catalogue in the caller's read-only snapshot.

    Exceptions are deliberately reduced to bounded unavailable evidence. No
    query error, unknown revision, or absent table grants bootstrap authority.
    """
    try:
        connection = db.connection()
        if connection.dialect.name != "postgresql":
            return _unavailable(authoritative=False)
        graph = _candidate_graph()
        relations = tuple(connection.execute(text(_RELATIONS_SQL)))
        schemas = frozenset(connection.execute(text(_SCHEMAS_SQL)).scalars())
        resolution_rows = tuple(connection.execute(text(_RELATION_RESOLUTION_SQL)))
        if (
            len(resolution_rows) != len(_REQUIREMENT_RELATIONS)
            or frozenset(row[0] for row in resolution_rows) != _REQUIREMENT_RELATIONS
            or any(
                public_oid != effective_oid
                for _, public_oid, effective_oid in resolution_rows
            )
        ):
            return _unavailable()
        objects_present = connection.execute(
            text(_NON_RELATIONAL_OBJECTS_SQL)
        ).scalar_one()
        if not isinstance(objects_present, bool):
            return _unavailable()
        columns: dict[str, frozenset[str]] = {}
        for table, column in connection.execute(text(_COLUMNS_SQL)):
            columns[table] = columns.get(table, frozenset()) | {column}
        catalogue = _Catalogue(
            columns,
            frozenset(
                (schema, name)
                for schema, name, _kind in relations
                if (schema, name) != ("public", "alembic_version")
            ),
            schemas,
            objects_present,
        )
        version_relation = next(
            (
                kind
                for schema, name, kind in relations
                if (schema, name) == ("public", "alembic_version")
            ),
            None,
        )
        if version_relation is not None and version_relation not in ("r", "p"):
            return _unavailable()
        current = (
            frozenset(
                connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                ).scalars()
            )
            if version_relation is not None
            else frozenset()
        )
        if current:
            return _classify_versioned(graph, current, catalogue)
        if catalogue.empty and _bootstrap_verified(query):
            return DeploymentSecretSchemaResult(
                SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP,
                SchemaCapabilityState.AUTHORIZED_EMPTY_BOOTSTRAP,
                False,
            )
        return _unavailable()
    except Exception:
        return _unavailable()
