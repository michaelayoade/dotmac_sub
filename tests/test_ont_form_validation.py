from uuid import uuid4

from starlette.datastructures import FormData

from app.services.network import ont_web_forms


def test_ont_create_payload_requires_olt_device_id():
    payload, error = ont_web_forms.build_ont_create_payload(
        FormData({"serial_number": "ONT-REQ-OLT-001"})
    )

    assert error == "Select an OLT for this ONT."
    assert payload is not None
    assert payload.serial_number == "ONT-REQ-OLT-001"
    assert payload.olt_device_id is None


def test_ont_create_payload_accepts_selected_olt():
    olt_id = uuid4()

    payload, error = ont_web_forms.build_ont_create_payload(
        FormData(
            {
                "serial_number": "ONT-REQ-OLT-002",
                "olt_device_id": str(olt_id),
            }
        )
    )

    assert error is None
    assert payload is not None
    assert payload.olt_device_id == olt_id
