"""Reviewed complaint correlation contracts owned by network.outage_lifecycle."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from app.services.action_readiness import ActionReadiness
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission


class OutageStatus(StrEnum):
    open = "open"
    suspected = "suspected"
    confirmed = "confirmed"
    clearing = "clearing"
    resolved = "resolved"
    discarded = "discarded"


OWNER = "network.outage_lifecycle"
CONCERN = "reviewed customer complaint incident correlation"
SUPPORT_READ = "support:ticket:read"
SUPPORT_WRITE = "support:ticket:update"
MONITORING_READ = "monitoring:read"
MONITORING_WRITE = "monitoring:write"


@dataclass(frozen=True, slots=True)
class ComplaintIncidentCandidatesQuery:
    ticket_id: UUID
    support_access: PermissionAdmission
    monitoring_access: PermissionAdmission
    after_incident_id: UUID | None = None
    limit: int = 30


@dataclass(frozen=True, slots=True)
class ComplaintIncidentCandidate:
    incident_id: UUID
    incident_status: OutageStatus
    incident_label: str
    reviewed_revision_id: UUID
    reviewed_revision_sequence: int
    membership_token: str
    subscription_id: UUID
    subscription_label: str
    membership: Literal["entered", "retained"]
    readiness: ActionReadiness
    needs_legacy_review: bool = False


@dataclass(frozen=True, slots=True)
class ComplaintIncidentCandidates:
    ticket_id: UUID
    ticket_label: str
    rows: tuple[ComplaintIncidentCandidate, ...]
    next_incident_id: UUID | None


@dataclass(frozen=True, slots=True)
class ComplaintIncidentLinkCommand:
    context: CommandContext
    support_access: PermissionAdmission
    monitoring_access: PermissionAdmission
    ticket_id: UUID
    incident_id: UUID
    subscription_id: UUID
    expected_revision_id: UUID
    expected_membership_token: str
    reason: str
    adopt_legacy: bool = False


@dataclass(frozen=True, slots=True)
class ComplaintIncidentLinkOutcome:
    link_id: UUID
    ticket_id: UUID
    incident_id: UUID
    reviewed_revision_id: UUID
    reviewed_revision_sequence: int
    subscription_id: UUID
    replayed: bool
    already_linked: bool


@dataclass(frozen=True, slots=True)
class RecordOutageScopeRevision:
    incident_id: UUID
    reason: str
    actor: str | None = None
    effective_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutageScopeRevisionRecorded:
    revision_id: UUID
    incident_id: UUID
    sequence: int
    membership_token: str
    appended: bool


@dataclass(frozen=True, slots=True)
class ComplaintIncidentLinkedEvent:
    link_id: UUID
    ticket_id: UUID
    incident_id: UUID
    subscription_id: UUID
    reviewed_revision_id: UUID
    reviewed_revision_sequence: int
    membership_token: str

    def payload(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "link_id": str(self.link_id),
            "ticket_id": str(self.ticket_id),
            "incident_id": str(self.incident_id),
            "subscription_id": str(self.subscription_id),
            "reviewed_revision_id": str(self.reviewed_revision_id),
            "reviewed_revision_sequence": self.reviewed_revision_sequence,
            "membership_token": self.membership_token,
        }


@dataclass(frozen=True, slots=True)
class ComplaintIncidentReceipt:
    """Native v2 proof of exact command material, link, and selected service."""

    fingerprint: str
    link_id: UUID
    subscription_id: UUID

    def encode(self) -> str:
        import base64
        import re

        if re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("Invalid complaint receipt fingerprint")

        def compact(value: UUID) -> str:
            return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")

        return f"v2:{self.fingerprint}:{compact(self.link_id)}:{compact(self.subscription_id)}"

    @classmethod
    def decode(cls, value: str) -> "ComplaintIncidentReceipt":
        import base64
        import re

        parts = value.split(":")
        if (
            len(parts) != 4
            or parts[0] != "v2"
            or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None
        ):
            raise ValueError("Invalid complaint receipt")

        def expand(part: str) -> UUID:
            if re.fullmatch(r"[A-Za-z0-9_-]{22}", part) is None:
                raise ValueError("Invalid complaint receipt UUID")
            raw = base64.b64decode(part + "==", altchars=b"-_", validate=True)
            result = UUID(bytes=raw)
            if (
                base64.urlsafe_b64encode(result.bytes).decode("ascii").rstrip("=")
                != part
            ):
                raise ValueError("Noncanonical complaint receipt UUID")
            return result

        return cls(
            fingerprint=parts[1],
            link_id=expand(parts[2]),
            subscription_id=expand(parts[3]),
        )
