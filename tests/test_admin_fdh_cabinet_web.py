from __future__ import annotations

from starlette.datastructures import FormData

from app.models.network import FdhCabinet, Splitter
from app.services import web_network_fdh


def test_fdh_list_paginates_all_active_cabinets(db_session):
    db_session.add_all(
        [
            FdhCabinet(name=f"FDH pagination {index:03d}", is_active=True)
            for index in range(55)
        ]
    )
    db_session.flush()

    first_page = web_network_fdh.list_page_data(db_session, page=1, per_page=10)
    last_page = web_network_fdh.list_page_data(db_session, page=6, per_page=10)

    assert first_page["stats"]["total"] == 55
    assert first_page["pagination"]["total"] == 55
    assert len(first_page["cabinets"]) == 10
    assert first_page["pagination"]["has_next"] is True
    assert len(last_page["cabinets"]) == 5
    assert last_page["pagination"]["has_next"] is False


def test_fdh_list_searches_name_and_code_in_active_scope(db_session):
    db_session.add_all(
        [
            FdhCabinet(name="Central cabinet", code="FDH-CENTRAL", is_active=True),
            FdhCabinet(name="Percent % cabinet", code="FDH-PERCENT", is_active=True),
            FdhCabinet(name="Retired cabinet", code="FDH-RETIRED", is_active=False),
        ]
    )
    db_session.flush()

    matched = web_network_fdh.list_page_data(db_session, search="central")
    retired = web_network_fdh.list_page_data(db_session, search="FDH-RETIRED")
    percent = web_network_fdh.list_page_data(db_session, search="%")

    assert matched["stats"]["total"] == 1
    assert matched["cabinets"][0].code == "FDH-CENTRAL"
    assert retired["stats"]["total"] == 0
    assert percent["stats"]["total"] == 1


def test_splitter_list_searches_its_cabinet_identity(db_session):
    cabinet = FdhCabinet(name="North hub", code="FDH-NORTH", is_active=True)
    db_session.add(cabinet)
    db_session.flush()
    db_session.add(
        Splitter(
            name="SPL-001",
            fdh_id=cabinet.id,
            splitter_ratio="1:8",
            input_ports=1,
            output_ports=8,
            is_active=True,
        )
    )
    db_session.flush()

    found = web_network_fdh.list_splitters_page_data(db_session, search="FDH-NORTH")
    assert found["stats"]["total"] == 1
    assert found["splitters"][0].fdh.name == "North hub"


def test_coordinate_validation_accepts_complete_wgs84_and_empty_pair():
    assert web_network_fdh.validate_coordinates("9.08", "7.49") is None
    assert web_network_fdh.validate_coordinates("0", "0") is None
    assert web_network_fdh.validate_coordinates("", "") is None


def test_coordinate_validation_rejects_partial_non_finite_and_out_of_range():
    assert web_network_fdh.validate_coordinates("9.08", "")
    assert web_network_fdh.validate_coordinates("nan", "7.49")
    assert web_network_fdh.validate_coordinates("91", "7.49")
    assert web_network_fdh.validate_coordinates("9.08", "181")


def test_fd_edit_refuses_unreviewed_coordinate_change(db_session):
    cabinet = FdhCabinet(
        name="FDH movement test", latitude=9.08, longitude=7.49, is_active=True
    )
    db_session.add(cabinet)
    db_session.flush()
    form = FormData(
        [
            ("name", cabinet.name),
            ("code", ""),
            ("region_id", ""),
            ("latitude", "9.09"),
            ("longitude", "7.50"),
            ("notes", ""),
            ("is_active", "true"),
        ]
    )

    result = web_network_fdh.update_cabinet_submission(
        db_session,
        cabinet,
        form,
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )

    assert "movement proposal" in result["error"]
    assert (cabinet.latitude, cabinet.longitude) == (9.08, 7.49)
