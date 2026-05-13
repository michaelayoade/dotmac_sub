from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

from app.models.catalog import AccessType, OfferStatus, PlanCategory
from app.services.network.profile_apply_workflow import (
    ProfileCommandGroup,
    build_profile_apply_plan,
)
from app.services.network.profile_inventory_preflight import (
    build_profile_inventory,
    validate_dotmac_profile_apply_plan,
    validate_offer_profile_sync_plan_inventory,
)
from app.services.network.profile_sync import build_offer_profile_sync_plan


@dataclass(frozen=True)
class ProfileEntry:
    profile_id: int
    name: str = ""


@dataclass(frozen=True)
class TrafficEntry:
    index: int
    name: str = ""


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


def test_profile_inventory_preflight_passes_for_free_dotmac_bundle() -> None:
    plan = build_offer_profile_sync_plan(
        _offer(),
        vlan_id=203,
        live_dba_profiles=[ProfileEntry(100, "SPL_DBA")],
        live_traffic_tables=[TrafficEntry(100, "SPL_TT")],
        live_line_profiles=[ProfileEntry(100, "SPL_LINE")],
        live_service_profiles=[ProfileEntry(100, "SPL_SRV")],
    )
    inventory = build_profile_inventory(
        dba_profiles=[ProfileEntry(100, "SPL_DBA")],
        traffic_tables=[TrafficEntry(100, "SPL_TT")],
        line_profiles=[ProfileEntry(100, "SPL_LINE")],
        service_profiles=[ProfileEntry(100, "SPL_SRV")],
    )

    result = validate_offer_profile_sync_plan_inventory(plan, inventory)

    assert result.success is True
    assert result.checked_ids["dba_profile_id"] == 101
    assert result.checked_names["dba"].startswith("DOTMAC_DBA_")


def test_profile_inventory_preflight_fails_on_existing_id_collision() -> None:
    plan = build_offer_profile_sync_plan(_offer(), vlan_id=203)
    inventory = build_profile_inventory(
        dba_profiles=[ProfileEntry(plan.bundle.dba_profile_id, "legacy")],
    )

    result = validate_offer_profile_sync_plan_inventory(plan, inventory)

    assert result.success is False
    assert "DBA profile ID 100 already exists" in result.message


def test_profile_inventory_preflight_fails_on_existing_name_collision() -> None:
    plan = build_offer_profile_sync_plan(_offer(), vlan_id=203)
    inventory = build_profile_inventory(
        dba_profiles=[ProfileEntry(999, result_name(plan, "dba"))],
    )

    result = validate_offer_profile_sync_plan_inventory(plan, inventory)

    assert result.success is False
    assert "DBA profile name" in result.message
    assert "already exists" in result.message


def test_profile_inventory_preflight_fails_on_malformed_inventory() -> None:
    class BadEntry:
        profile_id = "100"

    try:
        build_profile_inventory(dba_profiles=[BadEntry()])
    except ValueError as exc:
        assert "invalid profile_id" in str(exc)
    else:
        raise AssertionError("expected bad inventory to fail")


def test_dotmac_profile_apply_plan_allows_generated_create_commands() -> None:
    plan = build_offer_profile_sync_plan(_offer(), vlan_id=203).apply_plan

    result = validate_dotmac_profile_apply_plan(plan)

    assert result.success is True


def test_dotmac_profile_apply_plan_rejects_non_dotmac_profile_name() -> None:
    plan = build_profile_apply_plan(
        "legacy",
        [
            ProfileCommandGroup(
                step="Create DBA profile",
                commands=(
                    'dba-profile add profile-id 100 profile-name "LEGACY_DBA" type3 assure 20000 max 20000',
                ),
            ),
        ],
    )

    result = validate_dotmac_profile_apply_plan(plan)

    assert result.success is False
    assert "must start with DOTMAC_" in result.message


def test_dotmac_profile_apply_plan_rejects_destructive_command() -> None:
    plan = build_profile_apply_plan(
        "bad",
        [
            ProfileCommandGroup(
                step="Create DBA profile",
                commands=("undo dba-profile profile-id 100",),
            ),
        ],
    )

    result = validate_dotmac_profile_apply_plan(plan)

    assert result.success is False
    assert "create-only" in result.message


def result_name(plan, key: str) -> str:
    inventory = build_profile_inventory()
    result = validate_offer_profile_sync_plan_inventory(plan, inventory)
    return result.checked_names[key]
