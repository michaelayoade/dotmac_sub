import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.fiber_inquiry import FiberInquiryRequest


def _payload(**overrides):
    now = datetime.now(UTC).isoformat()
    payload = {
        "form_version": "fiber-coverage-v1",
        "full_name": "Coverage Prospect",
        "phone": "+2348031234567",
        "interest": "new_connection",
        "submitted_at": now,
        "attribution": {
            "journey_id": "70a978cc-60a6-4caa-af1d-b1328df690e4",
            "landing_path": "/coverage/",
            "captured_at": now,
        },
        "location": {"address": "12 Example Close, Wuse 2, Abuja"},
    }
    payload.update(overrides)
    return payload


def test_coverage_request_accepts_optional_area_email_and_plan():
    inquiry = FiberInquiryRequest.model_validate_json(json.dumps(_payload()))

    assert inquiry.location is not None
    assert inquiry.location.area is None
    assert inquiry.email is None
    assert inquiry.selected_plan is None


def test_coverage_request_preserves_area_and_plan_when_supplied():
    inquiry = FiberInquiryRequest.model_validate_json(
        json.dumps(
            _payload(
                location={
                    "address": "12 Example Close, Wuse 2, Abuja",
                    "area": "Wuse 2",
                    "latitude": "9.0765",
                    "longitude": "7.3986",
                },
                selected_plan={"name": "Home Elite"},
            )
        )
    )

    assert inquiry.location.area == "Wuse 2"
    assert inquiry.selected_plan is not None
    assert inquiry.selected_plan.name == "Home Elite"


def test_coverage_request_still_requires_installation_location():
    with pytest.raises(ValidationError, match="attribution and location"):
        FiberInquiryRequest.model_validate_json(json.dumps(_payload(location=None)))


def test_coverage_request_rejects_one_coordinate_without_the_other():
    with pytest.raises(ValidationError, match="supplied together"):
        FiberInquiryRequest.model_validate_json(
            json.dumps(
                _payload(
                    location={
                        "address": "12 Example Close, Wuse 2, Abuja",
                        "latitude": "9.0765",
                    }
                )
            )
        )
