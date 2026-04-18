from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.validators import catalog, network, provisioning, subscriber


@dataclass
class ValidationIssue:
    index: int
    detail: str


def _get(payload, key):
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key)


def validate_subscribers(db: Session, payloads: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, payload in enumerate(payloads):
        try:
            subscriber.validate_subscriber_email(
                _get(payload, "email"),
            )
            subscriber.validate_subscriber_name(
                _get(payload, "first_name"),
                _get(payload, "last_name"),
            )
        except HTTPException as exc:
            issues.append(ValidationIssue(index=idx, detail=str(exc.detail)))
    return issues


def validate_subscriptions(db: Session, payloads: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, payload in enumerate(payloads):
        try:
            catalog.validate_subscription_links(
                db,
                str(_get(payload, "account_id")),
                str(_get(payload, "offer_id")),
                str(_get(payload, "offer_version_id"))
                if _get(payload, "offer_version_id")
                else None,
                str(_get(payload, "service_address_id"))
                if _get(payload, "service_address_id")
                else None,
            )
        except HTTPException as exc:
            issues.append(ValidationIssue(index=idx, detail=str(exc.detail)))
    return issues


def validate_cpe_devices(db: Session, payloads: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, payload in enumerate(payloads):
        try:
            account_id = _get(payload, "account_id")
            service_address_id = _get(payload, "service_address_id")
            if account_id is not None:
                network.validate_cpe_device_links(
                    db,
                    str(account_id),
                    str(service_address_id) if service_address_id else None,
                )
            elif service_address_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="service_address_id requires account_id",
                )
        except HTTPException as exc:
            issues.append(ValidationIssue(index=idx, detail=str(exc.detail)))
    return issues


def validate_ip_assignments(db: Session, payloads: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, payload in enumerate(payloads):
        try:
            account_id = _get(payload, "account_id")
            service_address_id = _get(payload, "service_address_id")
            if account_id is not None:
                network.validate_ip_assignment_links(
                    db,
                    str(account_id),
                    str(service_address_id) if service_address_id else None,
                )
            elif service_address_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="service_address_id requires account_id",
                )
        except HTTPException as exc:
            issues.append(ValidationIssue(index=idx, detail=str(exc.detail)))
    return issues


def validate_service_orders(db: Session, payloads: list) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, payload in enumerate(payloads):
        try:
            provisioning.validate_service_order_links(
                db,
                str(_get(payload, "account_id")),
                str(_get(payload, "subscription_id"))
                if _get(payload, "subscription_id")
                else None,
                str(_get(payload, "requested_by_contact_id"))
                if _get(payload, "requested_by_contact_id")
                else None,
            )
        except HTTPException as exc:
            issues.append(ValidationIssue(index=idx, detail=str(exc.detail)))
    return issues
