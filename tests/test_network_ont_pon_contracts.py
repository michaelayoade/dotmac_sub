"""Network ONT/PON surfaces project the shared UI contracts.

The PON interface tiles used to be raw ``stats`` integers the template rendered
with no drill-down, and a modeled port monitoring had never observed showed a
bare "Unknown" string. They now come back as ``Kpi`` objects whose
``cohort_url`` filters the list to exactly the rows the tile counts
(KPI-parity), and each row's status is a ``StateValue`` so an unobserved port
reads as ``unknown`` state rather than a made-up reading. The ONT identity
review queue exposes each candidate's investigate eligibility as an ``Action``
owned by the backend, never re-derived from a status string in the template.
"""

from __future__ import annotations

from pathlib import Path

from app.models.network import OLTDevice, PonPort
from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)
from app.schemas.status_presentation import StatusTone
from app.services import web_network_pon_interfaces
from app.services.ui_contracts import Action, Kpi, StateKind, StateValue

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _seed_olt_with_interfaces(db_session) -> OLTDevice:
    olt = OLTDevice(name="OLT-K", mgmt_ip="203.0.113.10", is_active=True)
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            PonPort(
                olt_id=olt.id, name="0/1/0", notes="[[alias:Downtown]]", is_active=True
            ),
            PonPort(olt_id=olt.id, name="0/1/1", is_active=True),
        ]
    )
    monitor = NetworkDevice(name="OLT-K", mgmt_ip="203.0.113.10", is_active=True)
    db_session.add(monitor)
    db_session.flush()
    db_session.add_all(
        [
            DeviceInterface(
                device_id=monitor.id, name="gpon 0/1/0", status=InterfaceStatus.up
            ),
            DeviceInterface(
                device_id=monitor.id, name="gpon 0/1/1", status=InterfaceStatus.down
            ),
        ]
    )
    db_session.commit()
    return olt


def test_pon_kpis_are_contracts_that_link_to_their_cohort(db_session):
    _seed_olt_with_interfaces(db_session)
    data = web_network_pon_interfaces.build_page_data(db_session)
    kpis = data["pon_kpis"]

    assert set(kpis) == {"total", "up", "down", "unknown", "aliased"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert all(isinstance(kpi.value, StateValue) for kpi in kpis.values())

    # Each tile shows exactly the figure the scope stats carry (KPI-parity).
    for key in ("total", "up", "down", "unknown", "aliased"):
        assert kpis[key].value.value == data["stats"][key]

    # Every cohort URL is application-relative and drills into the right slice.
    for kpi in kpis.values():
        assert kpi.cohort_url.startswith("/admin/network/pon-interfaces")
    assert kpis["total"].cohort_url == "/admin/network/pon-interfaces"
    assert kpis["up"].cohort_url == "/admin/network/pon-interfaces?status=up"
    assert kpis["down"].cohort_url == "/admin/network/pon-interfaces?status=down"
    assert kpis["unknown"].cohort_url == "/admin/network/pon-interfaces?status=unknown"
    assert kpis["aliased"].cohort_url == "/admin/network/pon-interfaces?aliased=1"
    assert kpis["down"].tone is StatusTone.negative


def test_pon_kpi_cohort_preserves_active_filters(db_session):
    olt = _seed_olt_with_interfaces(db_session)
    data = web_network_pon_interfaces.build_page_data(
        db_session, search="0/1", olt_id=str(olt.id)
    )
    up_url = data["pon_kpis"]["up"].cohort_url
    assert up_url.startswith("/admin/network/pon-interfaces?")
    assert "status=up" in up_url
    assert "search=0%2F1" in up_url
    assert f"olt_id={olt.id}" in up_url


def test_pon_aliased_cohort_filters_to_aliased_rows(db_session):
    _seed_olt_with_interfaces(db_session)
    # The aliased tile counts 1; drilling its cohort must return that exact row.
    assert (
        web_network_pon_interfaces.build_page_data(db_session)["stats"]["aliased"] == 1
    )
    drilled = web_network_pon_interfaces.build_page_data(db_session, aliased="1")
    assert len(drilled["rows"]) == 1
    assert all(row["alias"] for row in drilled["rows"])


def test_pon_row_status_is_state_value(db_session):
    olt = OLTDevice(name="OLT-L", mgmt_ip="203.0.113.20", is_active=True)
    db_session.add(olt)
    db_session.flush()
    # A modeled port monitoring has never observed: no truth to render as a value.
    db_session.add(PonPort(olt_id=olt.id, name="0/2/0", is_active=True))
    db_session.commit()

    rows = web_network_pon_interfaces.build_page_data(db_session)["rows"]
    assert len(rows) == 1
    state = rows[0]["status_state"]
    assert isinstance(state, StateValue)
    assert state.kind is StateKind.unknown
    assert not state.is_present
    assert state.placeholder == "Unknown"


def test_pon_observed_status_is_present_state(db_session):
    _seed_olt_with_interfaces(db_session)
    rows = web_network_pon_interfaces.build_page_data(db_session, status="up")["rows"]
    assert rows
    for row in rows:
        assert row["status_state"].is_present
        assert row["status_state"].value == "Up"


def test_identity_candidate_propose_action_invariants():
    # Representative eligibility the review-queue owner builds: a fresh
    # investigation is allowed with no reason; an existing open decision blocks
    # it and must carry a non-empty reason (Action.__post_init__ enforces both).
    allowed = Action(
        key="propose_repair",
        label="Investigate",
        allowed=True,
        permission="network:fiber:write",
        tone=StatusTone.warning,
    )
    assert allowed.allowed is True and allowed.reason is None
    assert allowed.requires_confirmation is False and allowed.preview_url is None

    blocked = Action(
        key="propose_repair",
        label="Investigate",
        allowed=False,
        reason="An open repair decision already exists for this assignment",
        permission="network:fiber:write",
        tone=StatusTone.warning,
    )
    assert blocked.allowed is False and blocked.reason

    for bad in (
        dict(allowed=True, reason="no reason on an allowed action"),
        dict(allowed=False, reason=None),
        dict(allowed=False, reason="   "),
    ):
        try:
            Action(key="propose_repair", label="Investigate", **bad)
        except ValueError:
            pass
        else:  # pragma: no cover - the contract must reject these
            raise AssertionError(f"Action accepted invalid eligibility: {bad}")


def test_pon_template_renders_kpi_and_state_contracts():
    source = (_TEMPLATES / "admin/network/pon_interfaces/index.html").read_text(
        encoding="utf-8"
    )
    # Tiles deep-link and render the StateValue, not bare integers.
    assert "pon_kpis.total.value.value" in source
    assert "href=pon_kpis.up.cohort_url" in source
    assert "tone=pon_kpis.down.tone" in source
    # Row status renders through the StateValue's presence/placeholder.
    assert "row.status_state.is_present" in source
    assert "row.status_state.placeholder" in source


def test_identity_reviews_template_renders_action_eligibility():
    source = (_TEMPLATES / "admin/network/fiber/ont_identity_reviews.html").read_text(
        encoding="utf-8"
    )
    assert "action_permitted(request, candidate.propose_action)" in source
    assert "candidate.propose_action.label" in source
    assert "candidate.propose_action.reason" in source
