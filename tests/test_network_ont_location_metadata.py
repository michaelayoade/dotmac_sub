from types import SimpleNamespace

from app.schemas.network import OntUnitUpdate
from app.web.admin import network_onts as network_onts_admin


def test_location_modal_context_prefers_dedicated_contact(monkeypatch) -> None:
    monkeypatch.setattr(
        network_onts_admin.web_network_onts_service,
        "get_zones",
        lambda _db: [],
    )
    monkeypatch.setattr(
        network_onts_admin.web_network_onts_service,
        "get_splitters",
        lambda _db: [],
    )
    ont = SimpleNamespace(
        address_or_comment="123 Fiber St\n\n---\nLocation Contact: Legacy Contact",
        contact="Dedicated Contact",
        splitter_port_rel=None,
        zone_id=None,
        splitter_id=None,
        name="ONT-1",
        gps_latitude=None,
        gps_longitude=None,
    )

    context = network_onts_admin._location_modal_context(
        request=SimpleNamespace(),
        db=None,
        ont=ont,
    )

    assert context["form"]["address_or_comment"] == "123 Fiber St"
    assert context["form"]["contact"] == "Dedicated Contact"


def test_build_location_address_or_comment_drops_legacy_contact_encoding() -> None:
    value = network_onts_admin._build_location_address_or_comment(
        "123 Fiber St",
        "Dedicated Contact",
    )

    assert value == "123 Fiber St"


def test_ont_unit_update_accepts_contact() -> None:
    payload = OntUnitUpdate(contact="Dedicated Contact")

    assert payload.contact == "Dedicated Contact"
