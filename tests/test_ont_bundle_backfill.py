"""Post-migration tests for ONT bundle backfill verification.

The backfill module was designed to migrate ONTs from legacy columns to
bundle assignments + config overrides. After migration 064 dropped the
legacy columns, this module serves mainly as documentation and verification
that the backfill logic handles edge cases correctly.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.network import (
    OLTDevice,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntConfigOverrideSource,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
)
from app.services.network.ont_bundle_backfill import (
    build_backfill_plan,
    run_backfill,
)


def test_build_backfill_plan_marks_existing_assignment_as_already_migrated(db_session):
    """ONTs with an active bundle assignment are marked as already migrated."""
    olt = OLTDevice(name="OLT-Backfill-Existing", mgmt_ip="198.51.100.130", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Existing Bundle",
        olt_device_id=olt.id,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="BF-EXIST-001",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add_all([bundle, ont])
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "already_migrated"
    assert plan.bundle_id == str(bundle.id)


def test_build_backfill_plan_marks_ont_without_bundle_as_unconfigured(db_session):
    """ONTs without a bundle assignment and without provisioning history are unconfigured."""
    ont = OntUnit(
        serial_number="BF-UNCONFIG-001",
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "unconfigured"
    assert "no bundle and no legacy desired-state config" in plan.reason


def test_build_backfill_plan_flags_ont_with_provisioning_history_for_review(
    db_session,
):
    """ONTs with provisioning history but no bundle need manual review."""
    ont = OntUnit(
        serial_number="BF-PARTIAL-001",
        is_active=True,
        provisioning_status=OntProvisioningStatus.provisioned,
    )
    db_session.add(ont)
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "manual_review"
    assert "provisioning history" in plan.reason


def test_build_backfill_plan_flags_orphaned_overrides_for_review(db_session):
    """ONTs with overrides but no active assignment need manual review."""
    ont = OntUnit(
        serial_number="BF-ORPHAN-001",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()

    # Add an override without an active bundle assignment
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wifi.ssid",
            value_json={"value": "orphan-ssid"},
            source=OntConfigOverrideSource.operator,
            reason="test",
        )
    )
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "manual_review"
    assert "overrides without active assignment" in plan.reason


def test_run_backfill_classifies_onts_correctly(db_session):
    """run_backfill correctly classifies multiple ONTs."""
    # ONT with existing assignment
    olt = OLTDevice(name="OLT-Run", mgmt_ip="198.51.100.140", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Run Bundle",
        olt_device_id=olt.id,
        is_active=True,
    )
    migrated_ont = OntUnit(
        serial_number="BF-MIGRATED-001",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add_all([bundle, migrated_ont])
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=migrated_ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )

    # ONT without configuration
    unconfigured_ont = OntUnit(
        serial_number="BF-UNCONFIG-002",
        is_active=True,
    )
    db_session.add(unconfigured_ont)
    db_session.commit()

    result = run_backfill(db_session)
    by_serial = {plan.serial_number: plan for plan in result.plans}

    assert by_serial["BF-MIGRATED-001"].outcome == "already_migrated"
    assert by_serial["BF-UNCONFIG-002"].outcome == "unconfigured"
    assert result.counts["already_migrated"] >= 1
    assert result.counts["unconfigured"] >= 1
