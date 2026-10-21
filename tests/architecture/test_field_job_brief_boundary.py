"""Keep field briefs as scoped read composition over existing authorities."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import (
    owning_service_for,
    registry_validation_errors,
)

ROOT = Path(__file__).resolve().parents[2]


def test_brief_concerns_belong_to_existing_field_jobs_owner() -> None:
    assert registry_validation_errors() == ()
    for concern in (
        "assignment-scoped typed field job brief composition",
        "field-authorized issued and current outage audience presentation",
    ):
        owner = owning_service_for(concern)
        assert owner is not None and owner.name == "operations.field_jobs"
        assert {
            "network.outage_lifecycle",
            "network.outage_work_order_handoff",
            "operations.field_work_order_access",
        } <= set(owner.depends_on)


def test_read_capabilities_do_not_mutate_or_reresolve_outage_topology() -> None:
    for path in (
        "app/services/field/job_brief.py",
        "app/services/topology/outage_audience_read.py",
    ):
        source = (ROOT / path).read_text()
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls & {"add", "add_all", "commit", "flush", "delete", "rollback"}
        assert "require_work_order" in source
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "affected_customers"
            for node in ast.walk(tree)
        )
        assert "resolve_fdh_audience(" not in source
    audience = (ROOT / "app/services/topology/outage_audience_read.py").read_text()
    assert "latest_scope_revision(db, link.incident_id)" in audience
    assert "revision_audience_subscription_ids(revision)" in audience
    assert "OutageIncidentWorkOrderLink.work_order_id == work_order.id" in audience


def test_brief_customer_and_device_reads_use_explicit_column_allowlists() -> None:
    source = (ROOT / "app/services/field/job_brief.py").read_text()
    assert "select(NetworkDevice.name)" in source
    assert "db.get(NetworkDevice" not in source
    assert "db.get(Subscriber" not in source
    assert "Subscriber.billing_" not in source
    assert "Subscriber.nin" not in source
    assert "Subscriber.bvn" not in source
    assert "Subscriber.password" not in source
