from app.services import web_network_core_devices_views as core_devices_views


def test_normalize_port_name_uses_canonical_pon_hint() -> None:
    assert core_devices_views._normalize_port_name("GPON 0/1/0") == "0/1/0"
    assert core_devices_views._normalize_port_name("0/1/0") == "0/1/0"


def test_dedupe_live_board_inventory_collapses_duplicate_slots() -> None:
    deduped = core_devices_views._dedupe_live_board_inventory(
        [
            {
                "index": "101",
                "slot_number": 1,
                "card_type": "Control Board",
                "category": "card",
            },
            {
                "index": "202",
                "slot_number": 1,
                "card_type": "Main Control Board H901MPLA",
                "category": "card",
            },
            {
                "index": "303",
                "slot_number": 2,
                "card_type": "GPON Service Board",
                "category": "card",
            },
        ]
    )

    assert len(deduped) == 2
    assert deduped[0]["slot_number"] == 1
    assert deduped[0]["card_type"] == "Main Control Board H901MPLA"
    assert deduped[1]["slot_number"] == 2
