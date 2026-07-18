from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL = PROJECT_ROOT / "app/models/network.py"
CONNECTIVITY_MODEL = PROJECT_ROOT / "app/models/fiber_topology_connectivity.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/361_fiber_plant_operational_integrity.py"
CHANGE_OWNER = PROJECT_ROOT / "app/services/fiber_change_requests.py"
INTEGRITY = PROJECT_ROOT / "app/services/network/fiber_plant_integrity.py"
ACCESS_PATH = PROJECT_ROOT / "app/services/network/access_path.py"
FIBER_TOPOLOGY = PROJECT_ROOT / "app/services/fiber_topology.py"
WEB_FDH = PROJECT_ROOT / "app/services/web_network_fdh.py"


def _db_mutations(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
        and node.func.attr in {"add", "delete", "flush", "commit"}
    }


def test_operational_cable_shape_and_exact_core_identity_are_schema_enforced():
    model = MODEL.read_text(encoding="utf-8")
    connectivity_model = CONNECTIVITY_MODEL.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")

    for required in (
        "ck_fiber_segments_active_operational_shape",
        "ck_splitters_active_declared_capacity",
        "uq_fiber_strands_segment_strand",
        "fk_fiber_strands_segment",
        "ck_fiber_connectivity_create_capacity",
    ):
        assert required in migration
        assert (
            required in model
            or required in connectivity_model
            or required == "fk_fiber_strands_segment"
        )
    assert "fiber plant integrity preflight failed" in migration
    assert "UPDATE fiber_segments SET is_active" not in migration
    assert 'revision = "363_fiber_plant_operational_integrity"' in migration
    assert 'down_revision = "362_forwarding_topology_declarations"' in migration


def test_reviewed_change_owner_enforces_rootedness_capacity_and_safe_retirement():
    owner = CHANGE_OWNER.read_text(encoding="utf-8")
    integrity = INTEGRITY.read_text(encoding="utf-8")

    for required in (
        "validate_active_segment",
        "validate_segment_retirement",
        "validate_termination_change",
        "ensure_segment_strand_inventory",
        "from app.services.network.splitters import splitter_ports, splitters",
        "owner.create(db, payload, commit=False)",
        "owner.update(db, str(request.asset_id), payload, commit=False)",
        "owner.delete(db, str(request.asset_id), commit=False)",
    ):
        assert required in owner
    for required in (
        "serving PON/OLT root",
        "orphan an active cable component",
        "exact numbered fiber inventory",
        "splitter capacity cannot be smaller",
        "legacy cable-name strands require a reviewed exact segment ",
        "assignment before core inventory can be materialized",
    ):
        assert required in integrity
    for forbidden in ("ST_Distance", "nearest"):
        assert forbidden not in integrity


def test_admin_splitter_form_delegates_to_the_capacity_owner():
    source = WEB_FDH.read_text(encoding="utf-8")

    assert "splitter_service.create(" in source
    assert "splitter_service.update(" in source
    assert "SplitterCreate.model_validate" in source
    assert "SplitterUpdate.model_validate" in source
    assert "splitter = Splitter(" not in source


def test_composed_path_is_read_only_and_preserves_authority_boundaries():
    source = ACCESS_PATH.read_text(encoding="utf-8")

    assert _db_mutations(ACCESS_PATH) == set()
    for required in (
        "resolve_fiber_end_to_end_path",
        "trace_fiber_subscription",
        "project_authoritative_forwarding_graph",
        "provisioning_nas_device_id",
        "live_nas_state",
        "forwarding.nas_termination_declaration_missing",
        "capacity.cable_inventory_incomplete",
        "evidence_sha256",
    ):
        assert required in source
    for forbidden in (
        "ST_Distance",
        "nearest",
        "live_nas_id or expected_nas_id",
        "expected_nas_id or live_nas_id",
    ):
        assert forbidden not in source


def test_shared_segment_ranking_is_diagnostic_not_a_failure_writer():
    source = FIBER_TOPOLOGY.read_text(encoding="utf-8")

    assert '"shared_segment_candidate"' in source
    assert "absent from every complete" in source
    assert "fresh online comparison trace" in source
    assert "field-verification" in source
    assert "not a declaration" in source
    assert "cable has failed" in source
