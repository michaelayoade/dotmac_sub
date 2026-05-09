"""Tests for imported OLT profile admin helpers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    ServiceType,
)
from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltLineProfileGemMapping,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltProfileBundle,
    OltProfileSyncTask,
    OltServicePort,
    OltServiceProfile,
)
from app.services import web_network_olt_profiles
from app.services.network.olt_ssh_profiles import (
    DbaProfileEntry,
    OltProfileEntry,
    TrafficTableEntry,
)
from app.services.network.olt_state_import import (
    _import_line_profile_gem_mappings_from_config,
    _import_observed_service_ports_from_config,
    _import_service_port_gem_mappings_from_config,
)
from app.services.network.profile_apply_workflow import AppliedCommand


def test_imported_profile_state_context_returns_db_profiles(db_session):
    olt = OLTDevice(name="Imported Profiles OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltLineProfileGemMapping(
            olt_id=olt.id,
            line_profile_id=40,
            source="service_port",
            source_key="service-port:vlan:203:gem:1",
            gem_index=1,
            vlan_id=203,
            usage_count=10,
        )
    )
    db_session.flush()
    db_session.add(
        OltServicePort(
            olt_device_id=olt.id,
            port_index=42,
            fsp="0/1/7",
            ont_id_on_olt=5,
            vlan_id=203,
            gem_index=1,
            source="running_config",
            last_imported_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="EG8145V5",
            line_profile_id=40,
            service_profile_id=41,
        )
    )
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )

    assert context["error"] is None
    assert [profile.profile_id for profile in context["line_profiles"]] == [40]
    assert [profile.profile_id for profile in context["service_profiles"]] == [41]
    assert [mapping.equipment_id for mapping in context["profile_mappings"]] == [
        "EG8145V5"
    ]
    assert [mapping.gem_index for mapping in context["gem_mappings"]] == [1]
    assert context["service_port_summary"]["count"] == 1
    assert context["service_ports"][0].port_index == 42
    assert context["profile_bundles"] == []
    assert context["syncable_catalog_offers"] == []


def test_imported_profile_state_context_includes_syncable_offers(db_session):
    olt = OLTDevice(name="Imported Profiles Offers OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 50",
        code="F50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=50,
        speed_upload_mbps=20,
    )
    db_session.add_all([olt, offer])
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )

    assert [item.name for item in context["syncable_catalog_offers"]] == ["Fiber 50"]


def test_profile_sync_tasks_context_lists_open_tasks(db_session):
    olt = OLTDevice(name="Queue OLT", vendor="Huawei", mgmt_ip="10.0.0.10")
    offer = CatalogOffer(
        name="Fiber 75",
        code="F75",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=75,
        speed_upload_mbps=30,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    db_session.add(
        OltProfileSyncTask(
            olt_id=olt.id,
            offer_id=offer.id,
            status="pending",
            trigger="catalog_offer_update",
            requested_by="admin@example.test",
            preview_payload={
                "offer_name": offer.name,
                "vlan_id": 203,
                "download_kbps": 75_000,
                "upload_kbps": 30_000,
            },
        )
    )
    db_session.commit()

    context = web_network_olt_profiles.profile_sync_tasks_context(db_session)

    assert context["task_count"] == 1
    assert context["pending_count"] == 1
    assert context["open_count"] == 1
    assert context["tasks"][0].offer.name == "Fiber 75"
    assert context["tasks"][0].olt.name == "Queue OLT"


def test_approve_profile_sync_task_from_form_can_schedule(db_session):
    olt = OLTDevice(name="Schedule Queue OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 120",
        code="F120",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="pending",
        trigger="catalog_offer_update",
        preview_payload={"vlan_id": 203},
    )
    db_session.add(task)
    db_session.commit()

    ok, message = web_network_olt_profiles.approve_profile_sync_task_from_form(
        db_session,
        task_id=str(task.id),
        approved_by="admin@example.test",
        scheduled_for_raw="2026-05-10T09:30",
    )

    assert ok is True
    assert "Scheduled profile sync task" in message
    db_session.refresh(task)
    assert task.status == "scheduled"
    assert task.approved_by == "admin@example.test"
    assert task.scheduled_for is not None
    assert task.scheduled_for.replace(tzinfo=UTC) == datetime(
        2026, 5, 10, 9, 30, tzinfo=UTC
    )
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_sync_task_scheduled"
    assert event.entity_type == "olt_profile_sync_task"
    assert event.entity_id == str(task.id)
    assert event.actor_id == "admin@example.test"
    assert event.metadata_["status"] == "scheduled"
    assert event.metadata_["scheduled_for"].startswith("2026-05-10T09:30")


def test_cancel_profile_sync_task_from_form_marks_task_cancelled(db_session):
    olt = OLTDevice(name="Cancel Queue OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Cancel 120",
        code="FC120",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="approved",
        trigger="catalog_offer_update",
        preview_payload={"vlan_id": 203},
    )
    db_session.add(task)
    db_session.commit()

    ok, message = web_network_olt_profiles.cancel_profile_sync_task_from_form(
        db_session,
        task_id=str(task.id),
        cancelled_by="admin@example.test",
        reason="operator changed plan",
    )

    assert ok is True
    assert "Cancelled" in message
    db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.result_payload["cancelled_by"] == "admin@example.test"
    assert task.result_payload["cancel_reason"] == "operator changed plan"
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_sync_task_cancelled"
    assert event.entity_type == "olt_profile_sync_task"
    assert event.entity_id == str(task.id)
    assert event.actor_id == "admin@example.test"
    assert event.metadata_["status"] == "cancelled"
    assert event.metadata_["reason"] == "operator changed plan"


def test_execute_profile_sync_task_marks_completed_and_audits(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Execute Queue OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Execute 120",
        code="FE120",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    bundle = OltProfileBundle(
        olt_id=olt.id,
        offer_id=offer.id,
        name=offer.name,
        checksum="e" * 64,
        vlan_id=203,
        download_kbps=120_000,
        upload_kbps=60_000,
        dba_profile_id=100,
        download_traffic_table_id=101,
        upload_traffic_table_id=102,
        line_profile_id=103,
        service_profile_id=104,
        gem_id=1,
        tcont_id=1,
        command_plan={"groups": []},
        drift_status="pending",
        is_active=True,
    )
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="approved",
        trigger="catalog_offer_update",
        preview_payload={"vlan_id": 203},
    )
    db_session.add_all([bundle, task])
    db_session.commit()

    apply_result = SimpleNamespace(backup_id="backup-1", commands=[1, 2], errors=[])
    monkeypatch.setattr(
        web_network_olt_profiles,
        "apply_saved_profile_bundle",
        lambda *_args, **_kwargs: {
            "ok": True,
            "message": "applied",
            "apply_result": apply_result,
        },
    )

    ok, message = web_network_olt_profiles.execute_profile_sync_task(
        db_session,
        task_id=str(task.id),
        executed_by="admin@example.test",
        actor_is_admin=True,
    )

    assert ok is True
    assert message == "applied"
    db_session.refresh(task)
    assert task.status == "completed"
    assert task.result_payload["executed_by"] == "admin@example.test"
    assert task.result_payload["bundle_id"] == str(bundle.id)
    assert task.result_payload["backup_id"] == "backup-1"
    assert task.result_payload["commands"] == 2
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_sync_task_completed"
    assert event.entity_id == str(task.id)
    assert event.metadata_["status"] == "completed"
    assert event.metadata_["bundle_id"] == str(bundle.id)


def test_execute_profile_sync_task_marks_failed_without_bundle(db_session):
    olt = OLTDevice(name="Execute Missing Bundle OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Missing Bundle",
        code="FMB",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="approved",
        trigger="catalog_offer_update",
        preview_payload={"vlan_id": 203},
    )
    db_session.add(task)
    db_session.commit()

    ok, message = web_network_olt_profiles.execute_profile_sync_task(
        db_session,
        task_id=str(task.id),
        executed_by="admin@example.test",
        actor_is_admin=True,
    )

    assert ok is False
    assert "No active profile bundle" in message
    db_session.refresh(task)
    assert task.status == "failed"
    assert task.error == "No active profile bundle found for task"
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_sync_task_failed"
    assert event.is_success is False


def test_execute_due_profile_sync_tasks_runs_due_tasks_only(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Due Execute Queue OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Due Execute",
        code="FDE",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    bundle = OltProfileBundle(
        olt_id=olt.id,
        offer_id=offer.id,
        name=offer.name,
        checksum="f" * 64,
        vlan_id=203,
        download_kbps=120_000,
        upload_kbps=60_000,
        dba_profile_id=100,
        download_traffic_table_id=101,
        upload_traffic_table_id=102,
        line_profile_id=103,
        service_profile_id=104,
        gem_id=1,
        tcont_id=1,
        command_plan={"groups": []},
        drift_status="pending",
        is_active=True,
    )
    now = datetime(2026, 5, 10, 9, 30, tzinfo=UTC)
    approved = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="approved",
        trigger="catalog_offer_update",
    )
    due = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="scheduled",
        scheduled_for=now,
        trigger="catalog_offer_update",
    )
    future = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="scheduled",
        scheduled_for=datetime(2026, 5, 10, 10, 30, tzinfo=UTC),
        trigger="catalog_offer_update",
    )
    db_session.add_all([bundle, approved, due, future])
    db_session.commit()

    apply_result = SimpleNamespace(backup_id="backup-2", commands=[1], errors=[])
    monkeypatch.setattr(
        web_network_olt_profiles,
        "apply_saved_profile_bundle",
        lambda *_args, **_kwargs: {
            "ok": True,
            "message": "applied",
            "apply_result": apply_result,
        },
    )

    summary = web_network_olt_profiles.execute_due_profile_sync_tasks(
        db_session,
        executed_by="admin@example.test",
        actor_is_admin=True,
        now=now,
    )

    assert summary["total"] == 2
    assert summary["completed"] == 2
    assert summary["failed"] == 0
    db_session.refresh(approved)
    db_session.refresh(due)
    db_session.refresh(future)
    assert approved.status == "completed"
    assert due.status == "completed"
    assert future.status == "scheduled"


def test_retry_profile_sync_task_from_form_marks_pending_and_audits(db_session):
    olt = OLTDevice(name="Retry Queue OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Retry Queue",
        code="FRQ",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=120,
        speed_upload_mbps=60,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="failed",
        trigger="catalog_offer_update",
        error="preflight failed",
        result_payload={"executed_by": "admin@example.test"},
    )
    db_session.add(task)
    db_session.commit()

    ok, message = web_network_olt_profiles.retry_profile_sync_task_from_form(
        db_session,
        task_id=str(task.id),
        retried_by="admin@example.test",
        reason="inventory fixed",
    )

    assert ok is True
    assert "pending review" in message
    db_session.refresh(task)
    assert task.status == "pending"
    assert task.error is None
    assert task.result_payload["retries"][0]["previous_error"] == "preflight failed"
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_sync_task_retried"
    assert event.entity_id == str(task.id)
    assert event.metadata_["status"] == "pending"
    assert event.metadata_["reason"] == "inventory fixed"


def test_offer_profile_sync_preview_context_builds_dry_run_plan(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Preview OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 100",
        code="F100",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=100,
        speed_upload_mbps=50,
    )
    db_session.add_all([olt, offer])
    db_session.flush()

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        lambda _olt: (True, "ok", [DbaProfileEntry(profile_id=100)]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_traffic_tables",
        lambda _olt: (True, "ok", [TrafficTableEntry(index=100)]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_line_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=100, name="LINE")]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_service_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=100, name="SRV")]),
    )

    preview = web_network_olt_profiles.offer_profile_sync_preview_context(
        db_session,
        str(olt.id),
        offer_id=str(offer.id),
        vlan_id=203,
    )

    assert preview["ok"] is True
    assert preview["bundle"].download_kbps == 100_000
    assert preview["bundle"].dba_profile_id == 101
    assert preview["bundle"].download_traffic_table_id == 101
    assert preview["bundle"].upload_traffic_table_id == 102
    assert preview["apply_plan"].commands[0].startswith("dba-profile add")
    assert len(preview["allocations"]) == 5


def test_offer_profile_sync_preview_context_fails_when_live_inventory_fails(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Preview Failure OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 50",
        code="F50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=50,
        speed_upload_mbps=20,
    )
    db_session.add_all([olt, offer])
    db_session.flush()

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        lambda _olt: (False, "ssh timeout", []),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_traffic_tables",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_line_profiles",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_service_profiles",
        lambda _olt: (True, "ok", []),
    )

    preview = web_network_olt_profiles.offer_profile_sync_preview_context(
        db_session,
        str(olt.id),
        offer_id=str(offer.id),
        vlan_id=203,
    )

    assert preview["ok"] is False
    assert "DBA profiles: ssh timeout" in preview["message"]


def test_save_offer_profile_bundle_persists_validated_preview(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Save Bundle OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 150",
        code="F150",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=150,
        speed_upload_mbps=75,
    )
    db_session.add_all([olt, offer])
    db_session.flush()

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        lambda _olt: (True, "ok", [DbaProfileEntry(profile_id=100)]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_traffic_tables",
        lambda _olt: (True, "ok", [TrafficTableEntry(index=100)]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_line_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=100, name="LINE")]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_service_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=100, name="SRV")]),
    )

    preview = web_network_olt_profiles.save_offer_profile_bundle(
        db_session,
        str(olt.id),
        offer_id=str(offer.id),
        vlan_id=203,
    )

    assert preview["ok"] is True
    assert preview["saved_bundle"].id is not None
    assert preview["saved_bundle"].download_kbps == 150_000
    assert preview["saved_bundle"].command_plan["groups"][0]["step"] == (
        "Create DBA profile"
    )
    persisted = db_session.get(OltProfileBundle, preview["saved_bundle"].id)
    assert persisted is not None
    assert persisted.offer_id == offer.id


def test_imported_profile_state_context_returns_saved_profile_bundles(db_session):
    olt = OLTDevice(name="Saved Bundle Context OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 25",
        code="F25",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=25,
        speed_upload_mbps=10,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    db_session.add(
        OltProfileBundle(
            olt_id=olt.id,
            offer_id=offer.id,
            name="Fiber 25",
            checksum="a" * 64,
            vlan_id=203,
            download_kbps=25_000,
            upload_kbps=10_000,
            dba_profile_id=100,
            download_traffic_table_id=101,
            upload_traffic_table_id=102,
            line_profile_id=103,
            service_profile_id=104,
            gem_id=1,
            tcont_id=1,
            command_plan={"groups": []},
            drift_status="pending",
        )
    )
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )

    assert [bundle.name for bundle in context["profile_bundles"]] == ["Fiber 25"]


def test_apply_saved_profile_bundle_runs_backup_and_commands(db_session, monkeypatch):
    olt = OLTDevice(name="Apply Bundle OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 40",
        code="F40",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=40,
        speed_upload_mbps=20,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    bundle = OltProfileBundle(
        olt_id=olt.id,
        offer_id=offer.id,
        name="Fiber 40",
        checksum="b" * 64,
        vlan_id=203,
        download_kbps=40_000,
        upload_kbps=20_000,
        dba_profile_id=100,
        download_traffic_table_id=101,
        upload_traffic_table_id=102,
        line_profile_id=103,
        service_profile_id=104,
        gem_id=1,
        tcont_id=1,
        command_plan={
            "groups": [
                {
                    "step": "Create DBA profile",
                    "commands": [
                        'dba-profile add profile-id 100 profile-name "DOTMAC_DBA_F40" type3 assure 20000 max 20000'
                    ],
                    "requires_config_mode": True,
                }
            ]
        },
        drift_status="pending",
    )
    db_session.add(bundle)
    db_session.flush()
    calls: list[str] = []
    backup = SimpleNamespace(id=uuid4())

    def backup_runner(_db, olt_id):
        calls.append(f"backup:{olt_id}")
        return backup, "ok"

    def command_executor(_olt, plan):
        calls.append("execute")
        return [
            AppliedCommand(command=command, success=True, message="ok")
            for command in plan.commands
        ]

    monkeypatch.setattr(
        web_network_olt_profiles,
        "_validate_saved_bundle_against_live_inventory",
        lambda *_args: (True, "ok"),
    )

    result = web_network_olt_profiles.apply_saved_profile_bundle(
        db_session,
        str(olt.id),
        str(bundle.id),
        actor_is_admin=True,
        backup_runner=backup_runner,
        command_executor=command_executor,
    )

    assert result["ok"] is True
    assert calls == [f"backup:{olt.id}", "execute"]
    assert bundle.drift_status == "applied"
    assert bundle.last_applied_at is not None
    assert bundle.drift_details["backup_id"] == str(backup.id)


def test_check_profile_bundle_drift_updates_status_and_audits(db_session, monkeypatch):
    olt = OLTDevice(name="Drift Check OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 41",
        code="F41",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=41,
        speed_upload_mbps=20,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    bundle = OltProfileBundle(
        olt_id=olt.id,
        offer_id=offer.id,
        name="Fiber 41",
        checksum="d" * 64,
        vlan_id=203,
        download_kbps=41_000,
        upload_kbps=20_000,
        dba_profile_id=100,
        download_traffic_table_id=101,
        upload_traffic_table_id=102,
        line_profile_id=103,
        service_profile_id=104,
        gem_id=1,
        tcont_id=1,
        command_plan={
            "groups": [
                {
                    "step": "Create DBA profile",
                    "commands": [
                        'dba-profile add profile-id 100 profile-name "DOTMAC_DBA_F41" type3 assure 20000 max 20000'
                    ],
                },
                {
                    "step": "Create download traffic table",
                    "commands": [
                        'traffic table ip index 101 name "DOTMAC_TT_D_F41" cir 41000 pir 41000'
                    ],
                },
                {
                    "step": "Create upload traffic table",
                    "commands": [
                        'traffic table ip index 102 name "DOTMAC_TT_U_F41" cir 20000 pir 20000'
                    ],
                },
                {
                    "step": "Create line profile",
                    "commands": [
                        'ont-lineprofile gpon profile-id 103 profile-name "DOTMAC_LINE_F41"'
                    ],
                },
                {
                    "step": "Create service profile",
                    "commands": [
                        'ont-srvprofile gpon profile-id 104 profile-name "DOTMAC_SRV_F41"'
                    ],
                },
            ]
        },
        drift_status="pending",
    )
    db_session.add(bundle)
    db_session.flush()

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        lambda _olt: (True, "ok", [DbaProfileEntry(profile_id=100, name="DOTMAC_DBA_F41")]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_traffic_tables",
        lambda _olt: (
            True,
            "ok",
            [
                TrafficTableEntry(index=101, name="DOTMAC_TT_D_F41"),
                TrafficTableEntry(index=102, name="DOTMAC_TT_U_F41"),
            ],
        ),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_line_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=103, name="DOTMAC_LINE_F41")]),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_service_profiles",
        lambda _olt: (True, "ok", [OltProfileEntry(profile_id=104, name="DOTMAC_SRV_F41")]),
    )

    ok, message = web_network_olt_profiles.check_profile_bundle_drift(
        db_session,
        checked_by="admin@example.test",
    )

    assert ok is True
    assert "1 in sync" in message
    assert bundle.drift_status == "in_sync"
    assert bundle.last_verified_at is not None
    event = db_session.query(AuditEvent).one()
    assert event.action == "olt_profile_bundle_drift_checked"
    assert event.entity_type == "olt_profile_bundle"


def test_apply_saved_profile_bundle_requires_admin(db_session):
    olt = OLTDevice(name="Apply Bundle Admin OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber 45",
        code="F45",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=45,
        speed_upload_mbps=20,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    bundle = OltProfileBundle(
        olt_id=olt.id,
        offer_id=offer.id,
        name="Fiber 45",
        checksum="c" * 64,
        vlan_id=203,
        download_kbps=45_000,
        upload_kbps=20_000,
        dba_profile_id=100,
        download_traffic_table_id=101,
        upload_traffic_table_id=102,
        line_profile_id=103,
        service_profile_id=104,
        gem_id=1,
        tcont_id=1,
        command_plan={
            "groups": [
                {
                    "step": "Create DBA profile",
                    "commands": [
                        'dba-profile add profile-id 100 profile-name "DOTMAC_DBA_F45" type3 assure 20000 max 20000'
                    ],
                }
            ]
        },
        drift_status="pending",
    )
    db_session.add(bundle)
    db_session.flush()

    result = web_network_olt_profiles.apply_saved_profile_bundle(
        db_session,
        str(olt.id),
        str(bundle.id),
        actor_is_admin=False,
        backup_runner=lambda *_args: (_ for _ in ()).throw(AssertionError()),
    )

    assert result["ok"] is False
    assert "admin" in result["message"].lower()
    assert bundle.drift_status == "pending"


def test_save_imported_profile_mapping_requires_imported_profiles(db_session):
    olt = OLTDevice(name="Mapping Requires Profiles", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=40,
        service_profile_id=41,
    )

    assert ok is False
    assert "Line profile 40 has not been imported" in message


def test_save_imported_profile_mapping_upserts_explicit_mapping(db_session):
    olt = OLTDevice(name="Mapping Upsert OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltLineProfile(olt_id=olt.id, profile_id=50, name="LINE2"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
            OltServiceProfile(olt_id=olt.id, profile_id=51, name="EG8145V5-ALT"),
        ]
    )
    db_session.flush()

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=40,
        service_profile_id=41,
    )
    assert ok is True
    assert "Created mapping" in message

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=50,
        service_profile_id=51,
    )
    assert ok is True
    assert "Updated mapping" in message

    mappings = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )["profile_mappings"]
    assert len(mappings) == 1
    assert mappings[0].line_profile_id == 50
    assert mappings[0].service_profile_id == 51


def test_delete_imported_profile_mapping_scopes_to_olt(db_session):
    olt = OLTDevice(name="Mapping Delete OLT", vendor="Huawei")
    other_olt = OLTDevice(name="Other OLT", vendor="Huawei")
    db_session.add_all([olt, other_olt])
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="EG8145V5",
            line_profile_id=40,
            service_profile_id=41,
        )
    )
    db_session.flush()
    mapping = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )["profile_mappings"][0]

    ok, message = web_network_olt_profiles.delete_imported_profile_mapping(
        db_session,
        str(other_olt.id),
        str(mapping.id),
    )
    assert ok is False
    assert message == "Mapping not found"

    ok, message = web_network_olt_profiles.delete_imported_profile_mapping(
        db_session,
        str(olt.id),
        str(mapping.id),
    )
    assert ok is True
    assert "Deleted mapping" in message


def test_import_gem_mappings_from_lineprofile_and_service_ports(db_session):
    olt = OLTDevice(name="GEM Import OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add(
        OltLineProfile(olt_id=olt.id, profile_id=40, name="SMARTOLT_FLEXIBLE_GPON")
    )
    db_session.flush()
    config = """
ont-lineprofile gpon profile-id 40 profile-name "SMARTOLT_FLEXIBLE_GPON"
 gem add 1 eth tcont 1
 gem add 2 eth tcont 2
 gem mapping 1 1 priority 0
 gem mapping 2 1 vlan 201
 commit
 quit
interface gpon 0/1
 ont add 7 5 sn-auth "48575443348F8A84" omci ont-lineprofile-id 40 ont-srvprofile-id 41
 quit
service-port 10 vlan 203 gpon 0/1/7 ont 5 gemport 1 multi-service user-vlan 203 tag-transform translate
service-port 11 vlan 201 gpon 0/1/7 ont 5 gemport 2 multi-service user-vlan 201 tag-transform translate
"""
    imported_at = datetime.now(UTC)
    line_count = _import_line_profile_gem_mappings_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    db_session.add(
        OltOntRegistration(
            olt_id=olt.id,
            fsp="0/1/7",
            ont_id_on_olt=5,
            line_profile_id=40,
            service_profile_id=None,
            is_active=True,
        )
    )
    db_session.flush()
    service_count = _import_service_port_gem_mappings_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    observed_count = _import_observed_service_ports_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )
    rows = {
        (row.source, row.vlan_id, row.priority, row.gem_index)
        for row in context["gem_mappings"]
    }
    assert line_count == 2
    assert service_count == 2
    assert observed_count == 2
    observed_ports = {
        (row.port_index, row.vlan_id, row.gem_index)
        for row in db_session.query(OltServicePort).all()
    }
    assert (10, 203, 1) in observed_ports
    assert (11, 201, 2) in observed_ports
    assert ("line_profile", None, 0, 1) in rows
    assert ("line_profile", 201, None, 2) in rows
    assert ("service_port", 203, None, 1) in rows
    assert ("service_port", 201, None, 2) in rows
