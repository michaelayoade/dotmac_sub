"""Read-only interpretation of verified CRM customer observations.

The Integration Inbox owns the durable provider payload. This module may
identify an existing Subscriber only from exact retained CRM provenance; it
never creates or updates customer state and never completes a transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class CRMCustomerObservation:
    """Allowlisted identity provenance from one verified CRM receipt."""

    crm_person_id: str | None
    crm_quote_id: str | None
    crm_sales_order_id: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CRMCustomerObservation:
        return cls(
            crm_person_id=_text(payload.get("crm_person_id")),
            crm_quote_id=_text(payload.get("crm_quote_id")),
            crm_sales_order_id=_text(payload.get("crm_sales_order_id")),
        )


class CRMCustomerObservationStatus(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CRMCustomerObservationOutcome:
    """PII-free result of comparing CRM provenance with Sub records."""

    status: CRMCustomerObservationStatus
    subscriber_id: str | None = None
    subscriber_number: str | None = None
    account_number: str | None = None
    matched_via: tuple[str, ...] = ()

    def as_consequence(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "observed",
            "observation_status": self.status.value,
            "matched_via": list(self.matched_via),
        }
        if self.subscriber_id is not None:
            result.update(
                {
                    "id": self.subscriber_id,
                    "subscriber_id": self.subscriber_number or self.subscriber_id,
                    "subscriber_number": self.subscriber_number,
                    "account_number": self.account_number,
                }
            )
        return result


def _matches_for_identifier(
    db: Session,
    *,
    key: str,
    value: str,
) -> tuple[Subscriber, ...]:
    return tuple(
        db.query(Subscriber)
        .filter(Subscriber.metadata_[key].as_string() == value)
        .order_by(Subscriber.id)
        .limit(2)
        .all()
    )


def observe_customer(
    db: Session,
    observation: CRMCustomerObservation,
) -> CRMCustomerObservationOutcome:
    """Resolve exact provenance without inferring identity or changing state."""

    if observation.crm_person_id is not None:
        matches = _matches_for_identifier(
            db,
            key="crm_person_id",
            value=observation.crm_person_id,
        )
        if len(matches) > 1:
            return CRMCustomerObservationOutcome(
                status=CRMCustomerObservationStatus.AMBIGUOUS,
                matched_via=("crm_person_id",),
            )
        if len(matches) == 1:
            subscriber = matches[0]
            return CRMCustomerObservationOutcome(
                status=CRMCustomerObservationStatus.MATCHED,
                subscriber_id=str(subscriber.id),
                subscriber_number=subscriber.subscriber_number,
                account_number=subscriber.account_number,
                matched_via=("crm_person_id",),
            )
        return CRMCustomerObservationOutcome(
            status=CRMCustomerObservationStatus.UNMATCHED,
            matched_via=("crm_person_id",),
        )

    candidates: dict[UUID, Subscriber] = {}
    matched_via: list[str] = []
    for key, value in (
        ("crm_sales_order_id", observation.crm_sales_order_id),
        ("crm_quote_id", observation.crm_quote_id),
    ):
        if value is None:
            continue
        matches = _matches_for_identifier(db, key=key, value=value)
        if matches:
            matched_via.append(key)
        for subscriber in matches:
            candidates[subscriber.id] = subscriber

    if len(candidates) > 1:
        return CRMCustomerObservationOutcome(
            status=CRMCustomerObservationStatus.AMBIGUOUS,
            matched_via=tuple(matched_via),
        )
    if len(candidates) == 1:
        subscriber = next(iter(candidates.values()))
        return CRMCustomerObservationOutcome(
            status=CRMCustomerObservationStatus.MATCHED,
            subscriber_id=str(subscriber.id),
            subscriber_number=subscriber.subscriber_number,
            account_number=subscriber.account_number,
            matched_via=tuple(matched_via),
        )
    return CRMCustomerObservationOutcome(
        status=CRMCustomerObservationStatus.UNMATCHED,
        matched_via=tuple(matched_via),
    )
