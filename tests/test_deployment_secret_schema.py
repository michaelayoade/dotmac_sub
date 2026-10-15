"""Pure graph/predicate and simulated catalogue unit tests, not PG acceptance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.services import deployment_secret_schema as owner
from scripts.release_artifact_contract import (
    GitCommitSha,
    ProductionBootstrapAuthorization,
    ProductionServerName,
)
from scripts.release_candidate_evidence import write_bootstrap_authorization

STATE = owner.SchemaCapabilityState
SHA = GitCommitSha("a" * 40)
SERVER = ProductionServerName("dotmac-sub-prod")


@pytest.fixture
def graph(tmp_path: Path) -> ScriptDirectory:
    versions = tmp_path / "versions"
    versions.mkdir()
    revisions = (
        ("999_predecessor", None),
        (owner._INTEGRATION_ANCHOR, "999_predecessor"),
        ("550_before_machine", owner._INTEGRATION_ANCHOR),
        (owner._MACHINE_ANCHOR, "550_before_machine"),
        ("638_before_attribution", owner._MACHINE_ANCHOR),
        (owner._ATTRIBUTION_ANCHOR, "638_before_attribution"),
        ("001_latest", owner._ATTRIBUTION_ANCHOR),
    )
    for revision, parent in revisions:
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {parent!r}\n"
            "branch_labels = None\ndepends_on = None\n"
        )
    config = Config()
    config.set_main_option("script_location", str(tmp_path))
    return ScriptDirectory.from_config(config)


def _catalogue(
    *, machine: bool = False, attribution: bool = False, integrations: bool = False
) -> owner._Catalogue:
    columns: dict[str, frozenset[str]] = {}
    if integrations:
        columns.update(owner._INTEGRATION_COLUMNS)
    if machine:
        columns.update(owner._MACHINE_COLUMNS)
    if attribution:
        columns["machine_credentials"] |= {"source_application"}
    return owner._Catalogue(
        columns, frozenset(("public", name) for name in columns), frozenset()
    )


def test_numeric_prefix_is_never_revision_order(graph: ScriptDirectory) -> None:
    predecessor = owner._classify_versioned(
        graph, frozenset({"999_predecessor"}), _catalogue()
    )
    assert predecessor.ok
    assert predecessor.integrations is STATE.NOT_INSTALLED_AT_PREDECESSOR
    assert predecessor.machine_credentials is STATE.NOT_INSTALLED_AT_PREDECESSOR
    latest = owner._classify_versioned(
        graph,
        frozenset({"001_latest"}),
        _catalogue(integrations=True, machine=True, attribution=True),
    )
    assert latest.ok and latest.machine_attribution_available


@pytest.mark.parametrize("revision", [owner._MACHINE_ANCHOR, "638_before_attribution"])
def test_legacy_machine_capability_without_attribution(
    graph: ScriptDirectory, revision: str
) -> None:
    result = owner._classify_versioned(
        graph, frozenset({revision}), _catalogue(integrations=True, machine=True)
    )
    assert result.ok
    assert result.machine_credentials is STATE.EVALUATED
    assert not result.machine_attribution_available


def test_missing_attribution_after_anchor_is_drift(graph: ScriptDirectory) -> None:
    result = owner._classify_versioned(
        graph, frozenset({"001_latest"}), _catalogue(integrations=True, machine=True)
    )
    assert not result.ok
    assert result.machine_credentials is STATE.EVALUATION_UNAVAILABLE


def test_early_attribution_is_inconsistent(graph: ScriptDirectory) -> None:
    result = owner._classify_versioned(
        graph,
        frozenset({"638_before_attribution"}),
        _catalogue(integrations=True, machine=True, attribution=True),
    )
    assert not result.ok


@pytest.mark.parametrize("partial", [True, False])
def test_partial_or_missing_integrations_fail_after_introduction(
    graph: ScriptDirectory, partial: bool
) -> None:
    catalogue = _catalogue()
    if partial:
        catalogue.columns["integration_installations"] = owner._INTEGRATION_COLUMNS[
            "integration_installations"
        ]
    result = owner._classify_versioned(
        graph, frozenset({owner._INTEGRATION_ANCHOR}), catalogue
    )
    assert result.integrations is STATE.EVALUATION_UNAVAILABLE


def test_predecessor_with_partial_table_or_view_is_not_absence(
    graph: ScriptDirectory,
) -> None:
    catalogue = owner._Catalogue(
        {}, frozenset({("public", "integration_installations")}), frozenset()
    )
    assert not owner._classify_versioned(
        graph, frozenset({"999_predecessor"}), catalogue
    ).ok


def test_revision_alias_or_prefix_is_not_a_database_revision(
    graph: ScriptDirectory,
) -> None:
    for revision in ("head", "001"):
        assert not owner._classify_versioned(
            graph,
            frozenset({revision}),
            _catalogue(integrations=True, machine=True, attribution=True),
        ).ok


def test_version_set_must_be_frontier(graph: ScriptDirectory) -> None:
    result = owner._classify_versioned(
        graph,
        frozenset({"999_predecessor", "001_latest"}),
        _catalogue(integrations=True, machine=True, attribution=True),
    )
    assert not result.ok


def _session(
    *,
    relations: tuple[tuple[str, str, str], ...] = (),
    schemas: tuple[str, ...] = (),
    revisions: tuple[str, ...] = (),
    columns: tuple[tuple[str, str], ...] = (),
    objects_present: bool = False,
    resolutions: tuple[tuple[str, int | None, int | None], ...] | None = None,
) -> Session:
    connection = MagicMock(spec=Connection)
    connection.dialect = MagicMock(name="postgresql_dialect")
    connection.dialect.name = "postgresql"

    def execute(statement: object) -> MagicMock:
        sql = str(statement)
        result = MagicMock()
        if sql == owner._RELATIONS_SQL:
            result.__iter__.return_value = iter(relations)
        elif sql == owner._RELATION_RESOLUTION_SQL:
            result.__iter__.return_value = iter(
                resolutions
                if resolutions is not None
                else tuple((name, None, None) for name in owner._REQUIREMENT_RELATIONS)
            )
        elif sql == owner._SCHEMAS_SQL:
            result.scalars.return_value = iter(schemas)
        elif sql == owner._NON_RELATIONAL_OBJECTS_SQL:
            result.scalar_one.return_value = objects_present
        elif sql == owner._COLUMNS_SQL:
            result.__iter__.return_value = iter(columns)
        elif sql == "SELECT version_num FROM public.alembic_version":
            result.scalars.return_value = iter(revisions)
        else:
            raise AssertionError("unexpected catalogue query")
        return result

    connection.execute.side_effect = execute
    db = MagicMock(spec=Session)
    db.connection.return_value = connection
    return db


def _authorization(tmp_path: Path) -> owner.DeploymentSecretSchemaQuery:
    path = tmp_path / "bootstrap.json"
    write_bootstrap_authorization(
        path,
        ProductionBootstrapAuthorization(SHA, SERVER, "CHG-123", "first bootstrap"),
    )
    return owner.DeploymentSecretSchemaQuery(path, SHA, SERVER)


def test_bootstrap_requires_bound_document_and_empty_catalogue(
    graph: ScriptDirectory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    query = _authorization(tmp_path)
    result = owner.evaluate_deployment_secret_schema(_session(), query=query)
    assert result.ok and result.authoritative
    assert result.integrations is STATE.AUTHORIZED_EMPTY_BOOTSTRAP
    assert result.machine_credentials is STATE.AUTHORIZED_EMPTY_BOOTSTRAP
    assert not result.machine_attribution_available
    assert not owner.evaluate_deployment_secret_schema(
        _session(), query=owner.DeploymentSecretSchemaQuery()
    ).ok
    for mismatched in (
        owner.DeploymentSecretSchemaQuery(
            query.bootstrap_authorization_path, GitCommitSha("b" * 40), SERVER
        ),
    ):
        assert not owner.evaluate_deployment_secret_schema(
            _session(), query=mismatched
        ).ok


@pytest.mark.parametrize(
    "relations,schemas",
    [
        ((("public", "customers", "r"),), ()),
        ((("mod_future", "module_data", "r"),), ()),
        ((), ("mod_future",)),
    ],
)
def test_unversioned_application_catalogue_never_bootstraps(
    graph: ScriptDirectory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relations: tuple[tuple[str, str, str], ...],
    schemas: tuple[str, ...],
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    assert not owner.evaluate_deployment_secret_schema(
        _session(relations=relations, schemas=schemas), query=_authorization(tmp_path)
    ).ok


def test_query_failure_unknown_revision_and_bad_document_are_unavailable(
    graph: ScriptDirectory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    query = _authorization(tmp_path)
    db = _session()
    db.connection().execute.side_effect = RuntimeError("sensitive simulated failure")
    failure = owner.evaluate_deployment_secret_schema(db, query=query)
    assert not failure.ok and "sensitive" not in repr(failure)
    unknown = _session(
        relations=(("public", "alembic_version", "r"),), revisions=("unknown",)
    )
    assert not owner.evaluate_deployment_secret_schema(unknown, query=query).ok
    assert query.bootstrap_authorization_path is not None
    query.bootstrap_authorization_path.write_text("invalid authorization")
    assert not owner.evaluate_deployment_secret_schema(_session(), query=query).ok


def test_sqlite_metadata_never_establishes_authoritative_capability() -> None:
    with Session(create_engine("sqlite://")) as db:
        result = owner.evaluate_deployment_secret_schema(
            db, query=owner.DeploymentSecretSchemaQuery()
        )
        assert not result.ok and not result.authoritative


def test_candidate_graph_uses_actual_composed_lineage() -> None:
    graph = owner._candidate_graph()
    applied = owner._ancestors(graph, frozenset(graph.get_heads()))
    assert {
        owner._INTEGRATION_ANCHOR,
        owner._MACHINE_ANCHOR,
        owner._ATTRIBUTION_ANCHOR,
    } <= applied


def test_consumed_dependency_head_ancestry_is_complete(
    graph: ScriptDirectory,
) -> None:
    (Path(graph.dir) / "versions" / "dependent.py").write_text(
        "revision = 'dependent_head'\n"
        "down_revision = '999_predecessor'\n"
        "depends_on = '001_latest'\n"
        "branch_labels = None\n"
    )
    composed = ScriptDirectory(graph.dir)
    applied = owner._ancestors(composed, frozenset(composed.get_heads()))
    assert {
        "dependent_head",
        "001_latest",
        owner._INTEGRATION_ANCHOR,
        owner._MACHINE_ANCHOR,
        owner._ATTRIBUTION_ANCHOR,
    } <= applied


def test_composed_dependency_ancestry_is_feature_evidence(
    graph: ScriptDirectory,
) -> None:
    (Path(graph.dir) / "versions" / "module.py").write_text(
        "revision = 'module_head'\n"
        "down_revision = '999_predecessor'\n"
        "depends_on = '639_machine_attribution'\n"
        "branch_labels = None\n"
    )
    composed = ScriptDirectory(graph.dir)
    result = owner._classify_versioned(
        composed,
        frozenset({"module_head"}),
        _catalogue(integrations=True, machine=True, attribution=True),
    )
    assert result.ok and result.machine_attribution_available


@pytest.mark.parametrize(
    "column", ["tenant_id", "is_active", "revoked_at", "expires_at"]
)
def test_missing_machine_lifecycle_column_is_drift(
    graph: ScriptDirectory, column: str
) -> None:
    catalogue = _catalogue(integrations=True, machine=True, attribution=True)
    catalogue.columns["machine_credentials"] -= {column}
    assert not owner._classify_versioned(graph, frozenset({"001_latest"}), catalogue).ok


def test_non_relational_application_objects_refuse_bootstrap(
    graph: ScriptDirectory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    assert not owner.evaluate_deployment_secret_schema(
        _session(objects_present=True), query=_authorization(tmp_path)
    ).ok


def test_actual_candidate_graph_predecessors_and_head_predicates() -> None:
    graph = owner._candidate_graph()
    for anchor, catalogue in (
        (owner._INTEGRATION_ANCHOR, _catalogue()),
        (owner._MACHINE_ANCHOR, _catalogue(integrations=True)),
        (owner._ATTRIBUTION_ANCHOR, _catalogue(integrations=True, machine=True)),
    ):
        revision = graph.get_revision(anchor)
        assert revision is not None and isinstance(revision.down_revision, str)
        result = owner._classify_versioned(
            graph, frozenset({revision.down_revision}), catalogue
        )
        assert result.ok
        assert not result.machine_attribution_available
    head = owner._classify_versioned(
        graph,
        frozenset(graph.get_heads()),
        _catalogue(integrations=True, machine=True, attribution=True),
    )
    assert head.ok and head.machine_attribution_available


@pytest.mark.parametrize("table", sorted(owner._REQUIREMENT_RELATIONS))
@pytest.mark.parametrize("public_oid", [None, 123])
def test_shadow_resolution_is_unavailable_before_any_feature_query(
    graph: ScriptDirectory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    table: str,
    public_oid: int | None,
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    resolutions = tuple(
        (name, public_oid, 456) if name == table else (name, None, None)
        for name in owner._REQUIREMENT_RELATIONS
    )
    result = owner.evaluate_deployment_secret_schema(
        _session(resolutions=resolutions), query=_authorization(tmp_path)
    )
    assert not result.ok
    assert result.integrations is STATE.EVALUATION_UNAVAILABLE
    assert result.machine_credentials is STATE.EVALUATION_UNAVAILABLE
    assert "456" not in repr(result)


def test_incomplete_resolution_evidence_is_unavailable(
    graph: ScriptDirectory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    assert not owner.evaluate_deployment_secret_schema(
        _session(resolutions=()), query=_authorization(tmp_path)
    ).ok


def test_matching_public_resolution_allows_versioned_feature_assessment(
    graph: ScriptDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    catalogue = _catalogue(integrations=True, machine=True, attribution=True)
    relations = tuple(("public", name, "r") for name in owner._REQUIREMENT_RELATIONS)
    columns = tuple(
        (table, column)
        for table, names in catalogue.columns.items()
        for column in names
    )
    resolutions = tuple(
        (name, index, index)
        for index, name in enumerate(sorted(owner._REQUIREMENT_RELATIONS), start=1)
    )
    result = owner.evaluate_deployment_secret_schema(
        _session(
            relations=relations,
            columns=columns,
            revisions=("001_latest",),
            resolutions=resolutions,
        ),
        query=owner.DeploymentSecretSchemaQuery(),
    )
    assert result.ok and result.machine_attribution_available


def test_authorized_empty_version_table_is_bootstrap_not_feature_readiness(
    graph: ScriptDirectory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner, "_candidate_graph", lambda: graph)
    result = owner.evaluate_deployment_secret_schema(
        _session(
            relations=(("public", "alembic_version", "r"),),
            columns=(("alembic_version", "version_num"),),
            resolutions=tuple(
                (name, 123, 123) if name == "alembic_version" else (name, None, None)
                for name in owner._REQUIREMENT_RELATIONS
            ),
        ),
        query=_authorization(tmp_path),
    )
    assert result.ok
    assert result.integrations is STATE.AUTHORIZED_EMPTY_BOOTSTRAP
    assert result.machine_credentials is STATE.AUTHORIZED_EMPTY_BOOTSTRAP
    assert not result.machine_attribution_available
