from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
from app.models.network import OLTDevice, OltProfileBundle, OltProfileSyncTask
from app.services import web_network_olt_profiles
from app.services.network.profile_sync import (
    OfferProfileSyncError,
    OfferProfileSyncTaskError,
    OfferProfileSyncTaskRequest,
    approve_profile_sync_task,
    build_offer_profile_sync_plan,
    enqueue_offer_profile_sync_tasks,
    list_syncable_catalog_offers,
    upsert_profile_bundle,
)


@dataclass(frozen=True)
class ProfileEntry:
    profile_id: int


@dataclass(frozen=True)
class TrafficTableEntry:
    index: int


def _offer(**overrides):
    values = {
        "id": uuid4(),
        "name": "Home 50M",
        "code": "HOME-50",
        "status": OfferStatus.active,
        "is_active": True,
        "access_type": AccessType.fiber,
        "plan_category": PlanCategory.internet,
        "speed_download_mbps": 50,
        "speed_upload_mbps": 20,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_offer_profile_sync_plan_generates_bundle_and_commands() -> None:
    plan = build_offer_profile_sync_plan(
        _offer(),
        vlan_id=203,
        live_dba_profiles=[ProfileEntry(profile_id=100)],
        live_traffic_tables=[TrafficTableEntry(index=100)],
        live_line_profiles=[ProfileEntry(profile_id=100)],
        live_service_profiles=[ProfileEntry(profile_id=100)],
    )

    assert plan.bundle.download_kbps == 50_000
    assert plan.bundle.upload_kbps == 20_000
    assert plan.bundle.dba_profile_id == 101
    assert plan.bundle.download_traffic_table_id == 101
    assert plan.bundle.upload_traffic_table_id == 102
    assert plan.bundle.line_profile_id == 101
    assert plan.bundle.service_profile_id == 101
    assert len(plan.bundle.checksum) == 64
    assert plan.apply_plan.commands == (
        'dba-profile add profile-id 101 profile-name "DOTMAC_DBA_HOME-50_50D_20U" type3 assure 20000 max 20000',
        'traffic table ip index 101 name "DOTMAC_TT_D_HOME-50_50D_20U" cir 50000 pir 50000 priority 0',
        'traffic table ip index 102 name "DOTMAC_TT_U_HOME-50_50D_20U" cir 20000 pir 20000 priority 0',
        'ont-lineprofile gpon profile-id 101 profile-name "DOTMAC_LINE_HOME-50_50D_20U"',
        "tcont 1 dba-profile-id 101",
        "gem add 1 eth tcont 1",
        "gem mapping 1 0 vlan 203",
        "commit",
        "quit",
        'ont-srvprofile gpon profile-id 101 profile-name "DOTMAC_SRV_HOME-50_50D_20U"',
        "ont-port eth 4",
        "port vlan eth 1 203",
        "commit",
        "quit",
    )


def test_build_offer_profile_sync_plan_uses_offer_name_when_code_missing() -> None:
    plan = build_offer_profile_sync_plan(_offer(code=None, name="Biz Gold 100"), vlan_id=300)

    assert 'profile-name "DOTMAC_DBA_BIZ_GOLD_100_50D_20U"' in plan.apply_plan.commands[0]


def test_build_offer_profile_sync_plan_hashes_truncated_profile_names() -> None:
    first = build_offer_profile_sync_plan(
        _offer(
            code="ENTERPRISE-SYMMETRIC-FIBER-VERY-LONG-NAME-ALPHA",
            speed_download_mbps=100,
            speed_upload_mbps=50,
        ),
        vlan_id=203,
    )
    second = build_offer_profile_sync_plan(
        _offer(
            code="ENTERPRISE-SYMMETRIC-FIBER-VERY-LONG-NAME-BETA",
            speed_download_mbps=100,
            speed_upload_mbps=50,
        ),
        vlan_id=203,
    )

    first_name = first.apply_plan.commands[0].split('profile-name "')[1].split('"')[0]
    second_name = second.apply_plan.commands[0].split('profile-name "')[1].split('"')[0]
    assert first_name.startswith("DOTMAC_DBA_ENTERPRISE-SYMMETRIC-FIB")
    assert second_name.startswith("DOTMAC_DBA_ENTERPRISE-SYMMETRIC-FIB")
    assert first_name != second_name
    assert len(first_name) <= 64
    assert len(second_name) <= 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("is_active", False, "Only active offers"),
        ("status", OfferStatus.archived, "Only active offers"),
        ("access_type", AccessType.fixed_wireless, "Only fiber offers"),
        ("plan_category", PlanCategory.recurring, "Only internet offers"),
        ("speed_download_mbps", None, "speed_download_mbps"),
        ("speed_upload_mbps", 0, "speed_upload_mbps"),
    ],
)
def test_build_offer_profile_sync_plan_rejects_unsyncable_offers(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(OfferProfileSyncError, match=message):
        build_offer_profile_sync_plan(_offer(**{field: value}), vlan_id=203)


def test_build_offer_profile_sync_plan_rejects_bad_vlan() -> None:
    with pytest.raises(OfferProfileSyncError, match="vlan_id"):
        build_offer_profile_sync_plan(_offer(), vlan_id=0)


def test_list_syncable_catalog_offers_filters_active_fiber_internet_offers(db_session) -> None:
    syncable = CatalogOffer(
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
    no_speed = CatalogOffer(
        name="Fiber Missing Speed",
        code="FMS",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
    )
    wireless = CatalogOffer(
        name="Wireless 50",
        code="W50",
        service_type=ServiceType.residential,
        access_type=AccessType.fixed_wireless,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=50,
        speed_upload_mbps=20,
    )
    db_session.add_all([syncable, no_speed, wireless])
    db_session.commit()

    offers = list_syncable_catalog_offers(db_session)

    assert [offer.name for offer in offers] == ["Fiber 50"]


def test_upsert_profile_bundle_persists_generated_plan(db_session) -> None:
    olt = OLTDevice(name="Bundle OLT", vendor="Huawei")
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
    db_session.commit()

    plan = build_offer_profile_sync_plan(offer, vlan_id=203)
    record = upsert_profile_bundle(db_session, olt=olt, sync_plan=plan)
    db_session.commit()

    persisted = db_session.get(OltProfileBundle, record.id)
    assert persisted is not None
    assert persisted.olt_id == olt.id
    assert persisted.offer_id == offer.id
    assert persisted.checksum == plan.bundle.checksum
    assert persisted.command_plan["groups"][0]["step"] == "Create DBA profile"
    assert persisted.dba_profile_id == plan.bundle.dba_profile_id
    assert persisted.drift_status == "pending"


def test_upsert_profile_bundle_updates_existing_record(db_session) -> None:
    olt = OLTDevice(name="Bundle Update OLT", vendor="Huawei")
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
    db_session.commit()

    first_plan = build_offer_profile_sync_plan(offer, vlan_id=203)
    first = upsert_profile_bundle(db_session, olt=olt, sync_plan=first_plan)
    db_session.commit()

    offer.speed_download_mbps = 75
    offer.speed_upload_mbps = 30
    second_plan = build_offer_profile_sync_plan(offer, vlan_id=204)
    second = upsert_profile_bundle(db_session, olt=olt, sync_plan=second_plan)
    db_session.commit()

    assert second.id == first.id
    assert second.vlan_id == 204
    assert second.download_kbps == 75_000
    assert second.upload_kbps == 30_000
    assert second.checksum == second_plan.bundle.checksum


def test_apply_saved_profile_bundle_reruns_live_inventory_preflight(
    db_session,
    monkeypatch,
) -> None:
    olt = OLTDevice(name="Bundle Apply OLT", vendor="Huawei")
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
    db_session.commit()

    plan = build_offer_profile_sync_plan(offer, vlan_id=203)
    bundle = upsert_profile_bundle(db_session, olt=olt, sync_plan=plan)
    db_session.commit()
    calls: list[str] = []

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        lambda _olt: (
            True,
            "ok",
            [ProfileEntry(plan.bundle.dba_profile_id)],
        ),
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

    result = web_network_olt_profiles.apply_saved_profile_bundle(
        db_session,
        str(olt.id),
        str(bundle.id),
        actor_is_admin=True,
        backup_runner=lambda *_args: calls.append("backup"),  # type: ignore[arg-type,return-value]
        command_executor=lambda *_args: calls.append("execute"),  # type: ignore[arg-type,return-value]
    )

    assert result["ok"] is False
    assert "DBA profile ID" in result["message"]
    assert calls == []


def test_apply_saved_profile_bundle_rejects_non_admin_before_live_reads(
    db_session,
    monkeypatch,
) -> None:
    olt = OLTDevice(name="Bundle Non Admin OLT", vendor="Huawei")
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
    db_session.commit()

    plan = build_offer_profile_sync_plan(offer, vlan_id=203)
    bundle = upsert_profile_bundle(db_session, olt=olt, sync_plan=plan)
    db_session.commit()

    def fail_live_read(_olt):
        raise AssertionError("non-admin apply should not read live inventory")

    monkeypatch.setattr(
        web_network_olt_profiles.olt_ssh_profiles,
        "get_dba_profiles",
        fail_live_read,
    )

    result = web_network_olt_profiles.apply_saved_profile_bundle(
        db_session,
        str(olt.id),
        str(bundle.id),
        actor_is_admin=False,
    )

    assert result["ok"] is False
    assert result["message"] == "Only admin users can apply OLT profile bundles"


def test_enqueue_offer_profile_sync_tasks_requires_offer_flag(db_session) -> None:
    olt = OLTDevice(name="Auto Sync Disabled OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Auto Disabled",
        code="FAD",
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
        olt_profile_auto_sync_enabled=False,
    )
    db_session.add_all([olt, offer])
    db_session.commit()

    tasks = enqueue_offer_profile_sync_tasks(
        db_session,
        offer=offer,
        requests=[OfferProfileSyncTaskRequest(olt_id=str(olt.id), vlan_id=203)],
    )

    assert tasks == []
    assert db_session.query(OltProfileSyncTask).count() == 0


def test_enqueue_offer_profile_sync_tasks_creates_pending_review_task(db_session) -> None:
    olt = OLTDevice(name="Auto Sync OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Auto 100",
        code="FA100",
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
        olt_profile_auto_sync_enabled=True,
    )
    db_session.add_all([olt, offer])
    db_session.commit()

    first = enqueue_offer_profile_sync_tasks(
        db_session,
        offer=offer,
        requests=[OfferProfileSyncTaskRequest(olt_id=str(olt.id), vlan_id=203)],
        trigger="offer_update",
        requested_by="admin@example.test",
    )
    second = enqueue_offer_profile_sync_tasks(
        db_session,
        offer=offer,
        requests=[OfferProfileSyncTaskRequest(olt_id=str(olt.id), vlan_id=203)],
        trigger="offer_update",
        requested_by="admin@example.test",
    )
    db_session.commit()

    assert len(first) == 1
    assert second == []
    task = first[0]
    assert task.status == "pending"
    assert task.trigger == "offer_update"
    assert task.requested_by == "admin@example.test"
    assert task.preview_payload["mutates_olt"] is False
    assert task.preview_payload["requires_admin_preview"] is True
    assert task.preview_payload["vlan_id"] == 203
    assert task.preview_payload["download_kbps"] == 100_000
    assert db_session.query(OltProfileSyncTask).count() == 1


def test_approve_profile_sync_task_can_approve_or_schedule(db_session) -> None:
    olt = OLTDevice(name="Approve Sync OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Fiber Approve 100",
        code="FAP100",
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
        olt_profile_auto_sync_enabled=True,
    )
    db_session.add_all([olt, offer])
    db_session.commit()

    task = enqueue_offer_profile_sync_tasks(
        db_session,
        offer=offer,
        requests=[OfferProfileSyncTaskRequest(olt_id=str(olt.id), vlan_id=203)],
    )[0]

    approved = approve_profile_sync_task(
        db_session,
        task_id=str(task.id),
        approved_by="reviewer@example.test",
    )
    db_session.flush()

    assert approved.status == "approved"
    assert approved.approved_by == "reviewer@example.test"
    assert approved.approved_at is not None

    try:
        approve_profile_sync_task(
            db_session,
            task_id=str(task.id),
            approved_by="reviewer@example.test",
        )
    except OfferProfileSyncTaskError as exc:
        assert "Only pending" in str(exc)
    else:
        raise AssertionError("expected approved task to reject re-approval")
