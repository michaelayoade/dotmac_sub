"""Display imported fiber evidence separately from operator notes."""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FiberNotePresentation:
    text: str | None
    evidence: tuple[tuple[str, str], ...]


_EVIDENCE_LABELS = (
    ("source_system", "Source system"),
    ("source_profile", "Source profile"),
    ("source_external_id", "Source external ID"),
    ("fiber_topology_identity_decision_id", "Identity decision"),
    ("connectivity_decision_id", "Connectivity decision"),
    ("staged_feature_id", "Staged feature"),
    ("source_content_sha256", "Source fingerprint"),
)


def present_fiber_notes(notes: str | None) -> FiberNotePresentation:
    if not notes or not notes.strip():
        return FiberNotePresentation(None, ())
    try:
        parsed = json.loads(notes)
    except (ValueError, TypeError):
        return FiberNotePresentation(notes, ())
    if not isinstance(parsed, dict) or not (
        "source_system" in parsed or "source_profile" in parsed
    ):
        return FiberNotePresentation(notes, ())
    evidence = tuple(
        (label, str(parsed[key]))
        for key, label in _EVIDENCE_LABELS
        if parsed.get(key) is not None
    )
    return FiberNotePresentation(None, evidence)
