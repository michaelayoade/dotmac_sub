from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.services.auth_dependencies import require_user_auth
from app.services.field.voice import (
    clamp_confidence,
    extract_field_data,
    word_error_rate,
)


def _auth() -> dict:
    user_id = str(uuid4())
    return {
        "principal_id": user_id,
        "person_id": user_id,
        "subscriber_id": user_id,
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def test_voice_extracts_common_field_values():
    extraction = extract_field_data(
        "Job done, installed ONT serial hg8546m5, downstream signal minus 21 dB, "
        "used 40 metres drop cable"
    )

    assert extraction.work_status == "completed"
    assert extraction.equipment_serial == "HG8546M5"
    assert extraction.signal_readings["downstream"] == "-21 dB"
    assert extraction.materials_used == [
        {"name": "drop cable", "quantity": "40 metres"}
    ]
    assert extraction.confidence and extraction.confidence >= 0.8


def test_voice_quality_requires_review_for_low_signal_inputs():
    verdict = clamp_confidence(
        0.9,
        transcript="done",
        asr_confidence=0.4,
    )

    assert verdict.requires_review is True
    assert verdict.confidence == 0.3
    assert verdict.reasons == ["transcript_too_short", "low_asr_confidence"]


def test_word_error_rate_reports_token_distance():
    assert word_error_rate("install fibre drop", "install drop") == 1 / 3


def test_field_voice_api_route():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_user_auth] = _auth
    client = TestClient(app)

    response = client.post(
        "/api/v1/field/voice/extract",
        json={
            "transcript": "Router serial abc123 installed, rx minus 19 dbm",
            "asr_confidence": 0.95,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["equipment_serial"] == "ABC123"
    assert payload["signal_readings"]["rx"] == "-19 dBm"
    assert payload["requires_review"] is False
