from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MOBILE = PROJECT_ROOT / "field_mobile/lib"
DATABASE = MOBILE / "core/offline/database.dart"
MODELS = MOBILE / "features/jobs/work_order_evidence_map_models.dart"
REPOSITORY = MOBILE / "features/jobs/work_order_evidence_map_repository.dart"
SCREEN = MOBILE / "features/jobs/work_order_evidence_map_screen.dart"
JOB_DETAIL = MOBILE / "features/jobs/job_detail_screen.dart"
ROUTER = MOBILE / "app/router.dart"
SERVER_OWNER = (
    PROJECT_ROOT / "app/services/network/fiber_topology_work_order_evidence_map.py"
)


def test_mobile_evidence_cache_identity_is_exact_job_plus_report_hash():
    source = DATABASE.read_text()

    assert "class CachedWorkOrderEvidenceMaps extends Table" in source
    assert "TextColumn get principalScope" in source
    assert "TextColumn get workOrderPublicId" in source
    assert "TextColumn get reportSha256" in source
    assert "principalScope," in source
    assert "workOrderPublicId," in source
    assert "reportSha256," in source
    assert "int get schemaVersion => 5" in source
    assert "await m.createTable(cachedWorkOrderEvidenceMaps)" in source


def test_mobile_repository_is_get_only_and_never_falls_back_across_jobs():
    source = REPOSITORY.read_text()

    assert "'/api/v1/field/fiber/work-order-evidence-map'" in source
    assert ".get(" in source
    assert "'work_order_id': workOrderPublicId" in source
    assert ".post(" not in source
    assert ".patch(" not in source
    assert ".put(" not in source
    assert "requestedWorkOrderPublicId: workOrderPublicId" in source
    assert "row.workOrderPublicId.equals(workOrderPublicId)" in source
    assert "row.principalScope.equals(principalScope)" in source
    assert "jwtSubject(token)" in source
    assert "snapshot.reportSha256 != row.reportSha256" in source
    assert "on DioException" in source
    assert "statusCode == null || statusCode >= 500" in source


def test_mobile_model_fails_closed_and_preserves_server_owned_semantics():
    source = MODELS.read_text()
    server_source = SERVER_OWNER.read_text()

    assert "workOrderPublicId != requestedWorkOrderPublicId" in source
    assert "feature belongs to a different job" in source
    assert "workOrderEvidenceContexts.contains(context)" in source
    assert "workOrderEvidenceGeometryStates.contains(geometryState)" in source
    assert "contextPresentation.value != context" in source
    assert "geometryPresentation.value != geometryState" in source
    assert "Exact source geometry cannot be rendered without changing it" in source
    assert '"context_presentation"' in server_source
    assert '"geometry_presentation"' in server_source
    assert "StatusTone.warning" in server_source


def test_mobile_screen_renders_returned_cohort_without_topology_actions():
    source = SCREEN.read_text()

    assert "for (final feature in snapshot.features)" in source
    assert "feature.contextPresentation.tone" in source
    assert "Source overlay SHA-256" in source
    assert "Work-order evidence SHA-256" in source
    assert "This exact job report may be stale until refreshed" in source
    assert "No immutable fiber observations are attached to this job" in source
    for forbidden_call in (
        ".post(",
        ".patch(",
        ".put(",
        "createObservation(",
        "assignWorkOrder(",
        "repairGeometry(",
        "inferTopology(",
        "decideCustomerImpact(",
    ):
        assert forbidden_call not in source


def test_mobile_evidence_route_stays_nested_under_native_job_detail():
    router_source = ROUTER.read_text()
    detail_source = JOB_DETAIL.read_text()

    assert "path: '/jobs/:id/fiber-evidence'" in router_source
    assert "workOrderPublicId: state.pathParameters['id']!" in router_source
    assert "Key('open-fiber-evidence-map')" in detail_source
    assert "'/jobs/${Uri.encodeComponent(job.id)}/fiber-evidence'" in detail_source


def test_phase23_adds_no_backend_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("34[67]*fiber*evidence*mobile*.py")
    )
