"""Voice transcript extraction for field job forms.

The CRM implementation used the AI gateway for this flow. Until the shared AI
layer is migrated, sub keeps the mobile contract and quality gate while using a
conservative local extractor. It only pre-fills obvious values and marks
low-quality transcripts for manual review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_REVIEW_THRESHOLD = 0.7
_MIN_RELIABLE_WORDS = 3
_LOW_ASR_CONFIDENCE = 0.6
_HIGH_WER = 0.3


@dataclass(frozen=True)
class VoiceExtraction:
    work_status: str | None
    equipment_serial: str | None
    signal_readings: dict[str, str]
    materials_used: list[dict[str, str | None]]
    notes: str
    confidence: float | None


@dataclass(frozen=True)
class QualityVerdict:
    confidence: float
    requires_review: bool
    reasons: list[str] = field(default_factory=list)


def extract_field_data(
    transcript: str, *, context: str | None = None
) -> VoiceExtraction:
    normalized = _normalize_text(transcript)
    if not normalized:
        raise ValueError("Transcript is required")
    status = _extract_status(normalized)
    serial = _extract_serial(normalized)
    readings = _extract_signal_readings(normalized)
    materials = _extract_materials(normalized)
    confidence = _estimate_confidence(
        normalized,
        status=status,
        serial=serial,
        readings=readings,
        materials=materials,
        context=context,
    )
    return VoiceExtraction(
        work_status=status,
        equipment_serial=serial,
        signal_readings=readings,
        materials_used=materials,
        notes=normalized,
        confidence=confidence,
    )


def clamp_confidence(
    model_confidence: float | None,
    *,
    transcript: str,
    asr_confidence: float | None = None,
    estimated_wer: float | None = None,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> QualityVerdict:
    confidence = (
        0.5 if model_confidence is None else max(0.0, min(1.0, float(model_confidence)))
    )
    reasons: list[str] = []

    if len(_tokens(transcript)) < _MIN_RELIABLE_WORDS:
        confidence = min(confidence, 0.3)
        reasons.append("transcript_too_short")

    if asr_confidence is not None and asr_confidence < _LOW_ASR_CONFIDENCE:
        confidence = min(confidence, max(0.0, float(asr_confidence)))
        reasons.append("low_asr_confidence")

    if estimated_wer is not None and estimated_wer > _HIGH_WER:
        confidence = min(confidence, max(0.0, 1.0 - float(estimated_wer)))
        reasons.append("high_wer")

    confidence = max(0.0, min(1.0, confidence))
    return QualityVerdict(
        confidence=confidence,
        requires_review=confidence < review_threshold,
        reasons=reasons,
    )


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = _tokens(reference)
    hyp = _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    previous = list(range(len(hyp) + 1))
    for index, ref_token in enumerate(ref, start=1):
        current = [index] + [0] * len(hyp)
        for hyp_index, hyp_token in enumerate(hyp, start=1):
            cost = 0 if ref_token == hyp_token else 1
            current[hyp_index] = min(
                previous[hyp_index] + 1,
                current[hyp_index - 1] + 1,
                previous[hyp_index - 1] + cost,
            )
        previous = current
    return previous[len(hyp)] / len(ref)


def _extract_status(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\b(done|completed|complete|finished|closed)\b", lowered):
        return "completed"
    if re.search(r"\b(blocked|unable|cannot|can't|failed|no access)\b", lowered):
        return "blocked"
    if re.search(r"\b(started|working|in progress|ongoing|splicing)\b", lowered):
        return "in_progress"
    if re.search(r"\b(pending|waiting|hold)\b", lowered):
        return "pending"
    return None


def _extract_serial(text: str) -> str | None:
    match = re.search(
        r"\b(?:serial|sn|s/n|ont|onu|router|radio)\s*(?:number|no\.?|is|:)?\s*([a-z0-9][a-z0-9\-\s]{3,30})",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = re.split(
        r"\b(?:signal|used|installed|completed|done|at|with)\b",
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(
        r"^\s*(?:serial|sn|s/n|number|no\.?|is|:)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    serial = re.sub(r"[^a-zA-Z0-9-]", "", value).upper()
    return serial or None


def _extract_signal_readings(text: str) -> dict[str, str]:
    readings: dict[str, str] = {}
    pattern = re.compile(
        r"\b(?P<label>downstream|upstream|rx|tx|signal|power)\s*(?:signal|power|level|reading)?\s*(?:is|at|:)?\s*(?P<minus>minus|-)?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>dbm|db)?\b",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        label = match.group("label").lower()
        value = match.group("value")
        if match.group("minus"):
            value = f"-{value}"
        unit = (match.group("unit") or "dB").replace("dbm", "dBm").replace("db", "dB")
        readings[label] = f"{value} {unit}"
    return readings


def _extract_materials(text: str) -> list[dict[str, str | None]]:
    materials: list[dict[str, str | None]] = []
    pattern = re.compile(
        r"\b(?:used|consumed|installed|replaced)\s+(?P<quantity>\d+(?:\.\d+)?\s*(?:metres?|meters?|pieces?|pcs?|m)?)?\s*(?P<name>[a-z][a-z0-9\s\-]{2,40})",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        name = re.split(
            r"\b(?:and|with|then|signal|serial|done|completed)\b",
            match.group("name"),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        name = _normalize_text(name)
        quantity = _normalize_text(match.group("quantity")) or None
        if name and name.lower() not in {"ont", "onu", "router", "radio"}:
            materials.append({"name": name, "quantity": quantity})
    return materials[:10]


def _estimate_confidence(
    text: str,
    *,
    status: str | None,
    serial: str | None,
    readings: dict[str, str],
    materials: list[dict[str, str | None]],
    context: str | None,
) -> float:
    confidence = 0.55
    if len(_tokens(text)) >= 6:
        confidence += 0.1
    if context:
        confidence += 0.05
    if status:
        confidence += 0.08
    if serial:
        confidence += 0.08
    if readings:
        confidence += 0.08
    if materials:
        confidence += 0.06
    return round(min(confidence, 0.95), 2)


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _tokens(text: str) -> list[str]:
    cleaned = "".join(
        char.lower() if char.isalnum() or char.isspace() else " "
        for char in str(text or "")
    )
    return cleaned.split()
