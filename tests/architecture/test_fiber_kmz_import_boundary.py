from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMPORTER = PROJECT_ROOT / "scripts" / "network" / "import_fiber_kmz.py"
STAGING_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_staging.py"
)
IDENTITY_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_identity.py"
)
REVIEW_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_review.py"
)
CONNECTIVITY_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_connectivity.py"
)
ACCESS_ATTACHMENT_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_access_attachments.py"
)
FIBER_API = PROJECT_ROOT / "app" / "api" / "domains_network_fiber.py"
ACCESS_API = PROJECT_ROOT / "app" / "api" / "domains_network_access.py"
SPLITTER_SERVICE = PROJECT_ROOT / "app" / "services" / "network" / "splitters.py"
ONT_SERVICE = PROJECT_ROOT / "app" / "services" / "network" / "ont_crud.py"
NETWORK_MODELS = PROJECT_ROOT / "app" / "models" / "network.py"


def test_legacy_kmz_importer_is_preview_only() -> None:
    source = IMPORTER.read_text(encoding="utf-8")

    assert "Direct KMZ writes are retired" in source
    assert "--purge is retired" in source
    assert ".delete(" not in source
    assert ".commit(" not in source
    assert "db.rollback()" in source


def test_staging_owner_cannot_construct_or_delete_canonical_assets() -> None:
    source = STAGING_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
    ):
        assert constructor not in source

    assert "FiberTopologySourceBatch(" in source
    assert "FiberTopologyStagedFeature(" in source


def test_identity_owner_projects_creates_through_fiber_change_requests() -> None:
    source = IDENTITY_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
    ):
        assert constructor not in source

    assert "FiberTopologyIdentityDecision(" in source
    assert "FiberTopologyAssetSourceLink(" in source
    assert "fiber_change_requests.create_request(" in source


def test_review_owner_delegates_decisions_and_cannot_construct_assets() -> None:
    source = REVIEW_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
        "FiberTopologyIdentityDecision(",
        "FiberTopologyAssetSourceLink(",
    ):
        assert constructor not in source

    assert "FiberTopologyIdentityProposalBatch(" in source
    assert "FiberTopologyIdentityBatchReview(" in source
    assert "FiberTopologyIdentityExecutionRun(" in source
    assert "propose_identity_decision(" in source
    assert "approve_identity_decision(" in source
    assert "decline_identity_decision(" in source
    assert "execute_identity_decision(" in source
    assert "finalize_identity_decision(" in source


def test_connectivity_owner_requires_reviewed_requests_for_canonical_edges() -> None:
    source = CONNECTIVITY_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "FiberTerminationPoint(",
    ):
        assert constructor not in source

    assert "FiberTopologyConnectivityDecision(" in source
    assert "FiberTopologyTerminationResolution(" in source
    assert "FiberTopologySegmentSourceLink(" in source
    assert "fiber_change_requests.create_request(" in source


def test_direct_api_connectivity_mutations_are_retired() -> None:
    source = FIBER_API.read_text(encoding="utf-8")

    assert "Direct termination/segment mutation is retired" in source
    assert "fiber_termination_points.create(" not in source
    assert "fiber_termination_points.update(" not in source
    assert "fiber_termination_points.delete(" not in source
    assert "fiber_segments.create(" not in source
    assert "fiber_segments.update(" not in source
    assert "fiber_segments.delete(" not in source


def test_access_attachment_owner_is_the_only_canonical_attachment_writer() -> None:
    source = ACCESS_ATTACHMENT_OWNER.read_text(encoding="utf-8")

    assert "FiberAccessAttachmentDecision(" in source
    assert "PonPortSplitterLink(" in source
    assert "ont.splitter_port_id =" in source
    assert "ont.splitter_id =" in source
    for constructor in (
        "OLTDevice(",
        "OntUnit(",
        "PonPort(",
        "Splitter(",
        "SplitterPort(",
    ):
        assert constructor not in source
    for inferred_link in (
        "ST_Distance",
        "gps_latitude",
        "gps_longitude",
        "route_geom",
    ):
        assert inferred_link not in source

    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        candidate = path.read_text(encoding="utf-8")
        if path not in {ACCESS_ATTACHMENT_OWNER, NETWORK_MODELS}:
            assert "PonPortSplitterLink(" not in candidate
            assert "ont.splitter_port_id =" not in candidate
            assert "ont.splitter_id =" not in candidate


def test_direct_access_attachment_mutations_are_retired() -> None:
    api_source = ACCESS_API.read_text(encoding="utf-8")
    splitter_source = SPLITTER_SERVICE.read_text(encoding="utf-8")
    ont_source = ONT_SERVICE.read_text(encoding="utf-8")

    assert "Direct PON/splitter attachment mutation is retired" in api_source
    assert "pon_port_splitter_links.create(" not in api_source
    assert "pon_port_splitter_links.update(" not in api_source
    assert "pon_port_splitter_links.delete(" not in api_source
    assert "Direct PON/splitter attachment mutation is retired" in splitter_source
    assert "Direct ONT/splitter attachment mutation is retired" in ont_source
    assert '{"splitter_id", "splitter_port_id"}' in ont_source
