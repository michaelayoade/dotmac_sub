from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import domains_network_access
from app.models.fiber_access_attachment import (
    FiberAccessAttachmentDecision,
    SplitterCascadeLink,
)
from app.models.network import (
    OLTDevice,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)
from app.schemas.network import (
    OntUnitUpdate,
    PonPortSplitterLinkCreate,
    PonPortSplitterLinkUpdate,
)
from app.services import network as network_service
from app.services.network.fiber_access_attachments import (
    FiberAccessAttachmentError,
    approve_access_attachment,
    execute_access_attachment,
    preview_access_attachment,
    propose_access_attachment,
)


def _plant(db_session):
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(name=f"OLT {suffix}", hostname=f"olt-{suffix}", is_active=True)
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name=f"pon-{suffix}", is_active=True)
    splitter = Splitter(name=f"Splitter {suffix}", splitter_ratio="1:8", is_active=True)
    other_splitter = Splitter(
        name=f"Other splitter {suffix}", splitter_ratio="1:8", is_active=True
    )
    db_session.add_all([pon, splitter, other_splitter])
    db_session.flush()
    input_port = SplitterPort(
        splitter_id=splitter.id,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    first_output = SplitterPort(
        splitter_id=splitter.id,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    second_output = SplitterPort(
        splitter_id=splitter.id,
        port_number=2,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    other_input = SplitterPort(
        splitter_id=other_splitter.id,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    other_output = SplitterPort(
        splitter_id=other_splitter.id,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    db_session.add_all(
        [input_port, first_output, second_output, other_input, other_output]
    )
    db_session.flush()
    first_ont = OntUnit(
        serial_number=f"ONT-{suffix}-1",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    second_ont = OntUnit(
        serial_number=f"ONT-{suffix}-2",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add_all([first_ont, second_ont])
    db_session.commit()
    return {
        "olt": olt,
        "pon": pon,
        "splitter": splitter,
        "other_splitter": other_splitter,
        "input": input_port,
        "output_1": first_output,
        "output_2": second_output,
        "other_input": other_input,
        "other_output": other_output,
        "ont_1": first_ont,
        "ont_2": second_ont,
    }


def _review_and_execute(db_session, decision):
    approve_access_attachment(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Exact electronic and passive path verified",
    )
    return execute_access_attachment(
        db_session, decision.id, executed_by="executor@example.com"
    )


def _attach_pon(db_session, plant):
    decision = propose_access_attachment(
        db_session,
        "pon_input",
        "attach",
        plant["pon"].id,
        plant["input"].id,
        proposed_by="planner@example.com",
        reason="Field label and OLT plan independently verified",
    )
    return _review_and_execute(db_session, decision)


def test_pon_input_preview_is_read_only_and_execution_is_auditable(db_session):
    plant = _plant(db_session)

    preview = preview_access_attachment(
        db_session, "pon_input", "attach", plant["pon"].id, plant["input"].id
    )

    assert preview.olt_id == plant["olt"].id
    assert preview.previous_splitter_port_id is None
    assert db_session.query(FiberAccessAttachmentDecision).count() == 0
    assert db_session.query(PonPortSplitterLink).count() == 0

    decision = propose_access_attachment(
        db_session,
        "pon_input",
        "attach",
        plant["pon"].id,
        plant["input"].id,
        proposed_by="planner@example.com",
        reason="Field label and OLT plan independently verified",
    )
    replay = propose_access_attachment(
        db_session,
        "pon_input",
        "attach",
        plant["pon"].id,
        plant["input"].id,
        proposed_by="planner@example.com",
        reason="Field label and OLT plan independently verified",
    )
    assert replay.id == decision.id
    with pytest.raises(FiberAccessAttachmentError, match="proposer cannot review"):
        approve_access_attachment(
            db_session,
            decision.id,
            reviewed_by="planner@example.com",
            review_notes="Self review must fail",
        )

    applied = _review_and_execute(db_session, decision)
    link = db_session.query(PonPortSplitterLink).one()

    assert applied.status == "applied"
    assert link.active is True
    assert link.pon_port_id == plant["pon"].id
    assert link.splitter_port_id == plant["input"].id
    assert applied.result_payload["link_id"] == str(link.id)
    assert applied.result_payload["after_splitter_port_id"] == str(plant["input"].id)
    expected_digest = hashlib.sha256(
        json.dumps(
            applied.result_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert applied.result_sha256 == expected_digest
    assert (
        execute_access_attachment(
            db_session, decision.id, executed_by="another-executor@example.com"
        ).id
        == decision.id
    )


def test_port_direction_occupancy_and_same_splitter_are_enforced(db_session):
    plant = _plant(db_session)
    with pytest.raises(FiberAccessAttachmentError, match="active input"):
        preview_access_attachment(
            db_session,
            "pon_input",
            "attach",
            plant["pon"].id,
            plant["output_1"].id,
        )

    _attach_pon(db_session, plant)
    with pytest.raises(FiberAccessAttachmentError, match="active output"):
        preview_access_attachment(
            db_session,
            "ont_output",
            "attach",
            plant["ont_1"].id,
            plant["input"].id,
        )
    with pytest.raises(
        FiberAccessAttachmentError, match="splitter attached to its PON"
    ):
        preview_access_attachment(
            db_session,
            "ont_output",
            "attach",
            plant["ont_1"].id,
            plant["other_output"].id,
        )

    first = propose_access_attachment(
        db_session,
        "ont_output",
        "attach",
        plant["ont_1"].id,
        plant["output_1"].id,
        proposed_by="planner@example.com",
        reason="Customer drop and splitter output label verified",
    )
    applied = _review_and_execute(db_session, first)
    db_session.refresh(plant["ont_1"])

    assert applied.status == "applied"
    assert plant["ont_1"].splitter_port_id == plant["output_1"].id
    assert plant["ont_1"].splitter_id == plant["splitter"].id
    with pytest.raises(FiberAccessAttachmentError, match="another active ONT"):
        preview_access_attachment(
            db_session,
            "ont_output",
            "attach",
            plant["ont_2"].id,
            plant["output_1"].id,
        )


def test_ont_attachment_fails_closed_when_exact_pon_input_changes(db_session):
    plant = _plant(db_session)
    _attach_pon(db_session, plant)
    decision = propose_access_attachment(
        db_session,
        "ont_output",
        "attach",
        plant["ont_1"].id,
        plant["output_1"].id,
        proposed_by="planner@example.com",
        reason="Customer drop and splitter output label verified",
    )
    approve_access_attachment(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Exact path verified",
    )

    link = db_session.query(PonPortSplitterLink).one()
    link.splitter_port_id = plant["other_input"].id
    db_session.commit()
    closed = execute_access_attachment(
        db_session, decision.id, executed_by="executor@example.com"
    )
    db_session.refresh(plant["ont_1"])

    assert closed.status == "closed"
    assert closed.closed_reason == "authoritative_attachment_inputs_changed"
    assert closed.result_payload["outcome"] == "closed_stale"
    assert plant["ont_1"].splitter_port_id is None
    assert plant["ont_1"].splitter_id is None


def test_reviewed_detach_clears_port_and_denormalized_splitter(db_session):
    plant = _plant(db_session)
    _attach_pon(db_session, plant)
    attached = propose_access_attachment(
        db_session,
        "ont_output",
        "attach",
        plant["ont_1"].id,
        plant["output_1"].id,
        proposed_by="planner@example.com",
        reason="Customer drop and splitter output label verified",
    )
    _review_and_execute(db_session, attached)
    detach = propose_access_attachment(
        db_session,
        "ont_output",
        "detach",
        plant["ont_1"].id,
        proposed_by="fieldlead@example.com",
        reason="Field removal record verified",
    )
    applied = _review_and_execute(db_session, detach)
    db_session.refresh(plant["ont_1"])

    assert applied.status == "applied"
    assert applied.previous_splitter_port_id == plant["output_1"].id
    assert plant["ont_1"].splitter_port_id is None
    assert plant["ont_1"].splitter_id is None


def test_reviewed_cascade_is_exact_auditable_and_leaf_first(db_session):
    plant = _plant(db_session)
    plant["splitter"].insertion_loss_db = Decimal("3.500")
    plant["other_splitter"].insertion_loss_db = Decimal("4.000")
    db_session.commit()
    _attach_pon(db_session, plant)

    preview = preview_access_attachment(
        db_session,
        "splitter_cascade",
        "attach",
        plant["output_1"].id,
        plant["other_input"].id,
    )
    assert preview.upstream_splitter_id == plant["splitter"].id
    assert preview.splitter_id == plant["other_splitter"].id
    assert preview.splitter_stage == 2
    assert preview.cumulative_loss_db == Decimal("7.500")
    assert db_session.query(SplitterCascadeLink).count() == 0

    decision = propose_access_attachment(
        db_session,
        "splitter_cascade",
        "attach",
        plant["output_1"].id,
        plant["other_input"].id,
        proposed_by="planner@example.com",
        reason="Exact upstream output and downstream input labels verified",
    )
    applied = _review_and_execute(db_session, decision)
    link = db_session.query(SplitterCascadeLink).one()
    assert applied.status == "applied"
    assert link.active is True
    assert link.created_by_decision_id == decision.id
    assert applied.result_payload["link_id"] == str(link.id)
    assert applied.result_payload["splitter_stage"] == 2
    assert applied.result_payload["cumulative_loss_db"] == "7.500"

    ont_decision = propose_access_attachment(
        db_session,
        "ont_output",
        "attach",
        plant["ont_1"].id,
        plant["other_output"].id,
        proposed_by="planner@example.com",
        reason="Leaf output and customer drop verified",
    )
    _review_and_execute(db_session, ont_decision)
    with pytest.raises(FiberAccessAttachmentError, match="detach active ONTs"):
        preview_access_attachment(
            db_session,
            "splitter_cascade",
            "detach",
            plant["output_1"].id,
        )

    ont_detach = propose_access_attachment(
        db_session,
        "ont_output",
        "detach",
        plant["ont_1"].id,
        proposed_by="fieldlead@example.com",
        reason="Customer drop removal independently verified",
    )
    _review_and_execute(db_session, ont_detach)
    cascade_detach = propose_access_attachment(
        db_session,
        "splitter_cascade",
        "detach",
        plant["output_1"].id,
        proposed_by="fieldlead@example.com",
        reason="Cascade removal independently verified",
    )
    detached = _review_and_execute(db_session, cascade_detach)
    db_session.refresh(link)
    assert detached.status == "applied"
    assert link.active is False
    assert link.retired_by_decision_id == cascade_detach.id
    assert detached.result_payload["after_downstream_input_port_id"] is None


def test_cascade_rejects_cycles_and_output_role_conflicts(db_session):
    plant = _plant(db_session)
    plant["splitter"].insertion_loss_db = Decimal("3.500")
    plant["other_splitter"].insertion_loss_db = Decimal("4.000")
    db_session.commit()
    _attach_pon(db_session, plant)
    cascade = propose_access_attachment(
        db_session,
        "splitter_cascade",
        "attach",
        plant["output_1"].id,
        plant["other_input"].id,
        proposed_by="planner@example.com",
        reason="Exact cascade endpoints independently verified",
    )
    _review_and_execute(db_session, cascade)

    with pytest.raises(FiberAccessAttachmentError, match="create a cycle"):
        preview_access_attachment(
            db_session,
            "splitter_cascade",
            "attach",
            plant["other_output"].id,
            plant["input"].id,
        )
    with pytest.raises(
        FiberAccessAttachmentError, match="supplies a downstream splitter"
    ):
        preview_access_attachment(
            db_session,
            "ont_output",
            "attach",
            plant["ont_1"].id,
            plant["output_1"].id,
        )


def test_direct_attachment_writers_are_retired(db_session):
    plant = _plant(db_session)
    create_payload = PonPortSplitterLinkCreate(
        pon_port_id=plant["pon"].id,
        splitter_port_id=plant["input"].id,
        active=True,
    )
    with pytest.raises(HTTPException) as api_create:
        domains_network_access.create_pon_port_splitter_link(create_payload, db_session)
    assert api_create.value.status_code == 410

    with pytest.raises(HTTPException) as service_create:
        network_service.pon_port_splitter_links.create(db_session, create_payload)
    assert service_create.value.status_code == 410

    with pytest.raises(HTTPException) as service_update:
        network_service.pon_port_splitter_links.update(
            db_session,
            str(uuid.uuid4()),
            PonPortSplitterLinkUpdate(active=False),
        )
    assert service_update.value.status_code == 410

    with pytest.raises(HTTPException) as ont_update:
        network_service.ont_units.update(
            db_session,
            str(plant["ont_1"].id),
            OntUnitUpdate(splitter_port_id=plant["output_1"].id),
        )
    assert ont_update.value.status_code == 410
