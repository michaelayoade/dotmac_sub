from fastapi import APIRouter, Depends, HTTPException

from app.schemas.field import VoiceExtractRequest, VoiceExtractResponse
from app.services.auth_dependencies import require_user_auth
from app.services.field.voice import clamp_confidence, extract_field_data

router = APIRouter(prefix="/voice", tags=["field-voice"])


@router.post("/extract", response_model=VoiceExtractResponse)
def extract_voice(
    payload: VoiceExtractRequest,
    _auth: dict = Depends(require_user_auth),
):
    try:
        extraction = extract_field_data(
            payload.transcript,
            context=payload.context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    verdict = clamp_confidence(
        extraction.confidence,
        transcript=payload.transcript,
        asr_confidence=payload.asr_confidence,
    )
    return VoiceExtractResponse(
        work_status=extraction.work_status,
        equipment_serial=extraction.equipment_serial,
        signal_readings=extraction.signal_readings,
        materials_used=extraction.materials_used,
        notes=extraction.notes,
        confidence=verdict.confidence,
        requires_review=verdict.requires_review,
        review_reasons=verdict.reasons,
    )
