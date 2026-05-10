from types import SimpleNamespace

from app.models.audit import AuditActorType, AuditEvent
from app.models.network import CPEDevice, DeviceGroupMember, OntAssignment, OntUnit
from app.services.network import device_groups


def test_device_group_adds_ont_member_once(db_session):
    group = device_groups.create_device_group(
        db_session,
        name="North POP ONTs",
        created_by="admin",
    )
    ont = OntUnit(serial_number="DG-ONT-001", is_active=True)
    db_session.add(ont)
    db_session.flush()

    first = device_groups.add_device_group_member(
        db_session,
        group_id=group.id,
        device_type="ont",
        device_id=ont.id,
        added_by="admin",
    )
    second = device_groups.add_device_group_member(
        db_session,
        group_id=group.id,
        device_type="ont",
        device_id=ont.id,
        added_by="admin",
    )

    assert first.id == second.id
    assert (
        db_session.query(DeviceGroupMember)
        .filter(DeviceGroupMember.group_id == group.id)
        .count()
        == 1
    )


def test_enqueue_ont_group_action_queues_existing_bulk_task(db_session, monkeypatch):
    group = device_groups.create_device_group(db_session, name="Reboot Cohort")
    ont = OntUnit(serial_number="DG-ONT-002", is_active=True)
    db_session.add(ont)
    db_session.flush()
    db_session.add(OntAssignment(ont_unit_id=ont.id, active=True))
    db_session.flush()
    device_groups.add_device_group_member(
        db_session,
        group_id=group.id,
        device_type="ont",
        device_id=ont.id,
    )

    calls = []

    def fake_delay(ont_ids, action, params):
        calls.append((ont_ids, action, params))
        return SimpleNamespace(id="task-1")

    from app.tasks import ont_bulk

    monkeypatch.setattr(ont_bulk.execute_bulk_action, "delay", fake_delay)

    result = device_groups.enqueue_ont_group_action(
        db_session,
        group_id=group.id,
        action="reboot",
        initiated_by="admin",
    )

    assert result["task_id"] == "task-1"
    assert result["ont_count"] == 1
    assert calls == [([str(ont.id)], "reboot", {"initiated_by": "admin"})]


def test_device_group_detail_context_includes_candidates_and_history(db_session):
    group = device_groups.create_device_group(db_session, name="Audit Cohort")
    included = OntUnit(serial_number="DG-INCLUDED", is_active=True)
    candidate = OntUnit(serial_number="DG-CANDIDATE", is_active=True, model="HG8245")
    cpe = CPEDevice(serial_number="DG-CPE-001", mac_address="00:11:22:33:44:55")
    db_session.add_all([included, candidate, cpe])
    db_session.flush()
    device_groups.add_device_group_member(
        db_session,
        group_id=group.id,
        device_type="ont",
        device_id=included.id,
    )
    db_session.add(
        AuditEvent(
            actor_type=AuditActorType.user,
            actor_id="admin",
            action="device_group_action_queued",
            entity_type="device_group",
            entity_id=str(group.id),
            is_success=True,
            metadata_={"task_id": "task-1"},
        )
    )
    db_session.flush()

    context = device_groups.device_group_detail_context(db_session, group.id)

    assert [item["label"] for item in context["ont_candidates"]] == ["DG-CANDIDATE"]
    assert [item["label"] for item in context["cpe_candidates"]] == ["DG-CPE-001"]
    assert context["action_events"][0].action == "device_group_action_queued"


def test_device_group_update_and_archive(db_session):
    group = device_groups.create_device_group(db_session, name="Old Name")

    updated = device_groups.update_device_group(
        db_session,
        group_id=group.id,
        name="New Name",
        description="Updated",
    )
    archived = device_groups.archive_device_group(db_session, group_id=group.id)

    assert updated.name == "New Name"
    assert updated.description == "Updated"
    assert archived.is_active is False
