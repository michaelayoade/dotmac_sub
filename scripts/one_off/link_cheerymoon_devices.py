#!/usr/bin/env python3
"""Link Cheerymoon UISP ONTs to account 222 / 100000222."""

from __future__ import annotations

from app.db import SessionLocal
from app.models.network import OntAssignment
from app.schemas.network import OntAssignmentCreate
from app.services.network import ont_assignments

SUBSCRIBER_ID = "1b42f969-a985-4e24-baf5-d15c2e1b9ccf"
LINKS = [
    (
        "6cb01996-08e5-479b-a816-e09431f7adac",
        "4da8977a-5219-47e0-8f7b-4f73d206c8eb",
        "UISP name match: Cheerymoon global concept Ltd",
    ),
    (
        "0b1a2a77-46b1-477c-905c-e20e37aea005",
        "72899b43-85dc-4f27-91d0-3953d6cdfa5b",
        "UISP name match: Cheerymoon global concept",
    ),
]


def main() -> None:
    db = SessionLocal()
    try:
        for ont_id, pon_port_id, note in LINKS:
            existing = (
                db.query(OntAssignment)
                .filter(OntAssignment.ont_unit_id == ont_id)
                .filter(OntAssignment.active.is_(True))
                .one_or_none()
            )
            if existing is not None:
                print(
                    "existing",
                    existing.id,
                    existing.ont_unit_id,
                    existing.subscriber_id,
                    existing.pon_port_id,
                    existing.active,
                )
                continue
            payload = OntAssignmentCreate(
                ont_unit_id=ont_id,
                pon_port_id=pon_port_id,
                account_id=SUBSCRIBER_ID,
                active=True,
                notes=f"Linked to account 222 / 100000222 on 2026-07-07; {note}.",
            )
            assignment = ont_assignments.create(db, payload)
            print(
                assignment.id,
                assignment.ont_unit_id,
                assignment.subscriber_id,
                assignment.pon_port_id,
                assignment.active,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
