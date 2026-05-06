"""Tests for Huawei OLT profile resolution helpers."""

from app.services.network.olt_profile_resolution import (
    OntCapabilityCounts,
    ServiceProfileDetail,
    choose_service_profile,
)


def test_choose_service_profile_prefers_model_name_over_generic_count_match():
    profiles = [
        ServiceProfileDetail(
            profile_id=40,
            name="ONU-type-eth-4-pots-2-catv-0",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=120,
        ),
        ServiceProfileDetail(
            profile_id=41,
            name="EG8145V5",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=20,
        ),
    ]

    selected = choose_service_profile(
        profiles,
        capability=OntCapabilityCounts(ethernet_ports=4, voip_ports=2, catv_ports=0),
        model="EG8145V5",
    )

    assert selected is not None
    assert selected.profile_id == 41


def test_choose_service_profile_uses_capability_when_model_is_unknown():
    profiles = [
        ServiceProfileDetail(
            profile_id=40,
            name="ONU-type-eth-4-pots-2-catv-0",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=20,
        ),
        ServiceProfileDetail(
            profile_id=45,
            name="ONU-type-eth-4-pots-1-catv-1",
            ethernet_ports=4,
            voip_ports=1,
            catv_ports=1,
            binding_count=1,
        ),
    ]

    selected = choose_service_profile(
        profiles,
        capability=OntCapabilityCounts(ethernet_ports=4, voip_ports=1, catv_ports=1),
    )

    assert selected is not None
    assert selected.profile_id == 45
