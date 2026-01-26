from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.person import Person
from app.models.subscriber import Organization
from app.services.response import list_response


def search(db: Session, query: str, limit: int = 20) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    people = (
        db.query(Person)
        .filter(
            or_(
                Person.first_name.ilike(like_term),
                Person.last_name.ilike(like_term),
                Person.email.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    organizations = (
        db.query(Organization)
        .filter(
            or_(
                Organization.name.ilike(like_term),
                Organization.domain.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    items: list[dict] = []
    for person in people:
        label = f"{person.first_name} {person.last_name}"
        if person.email:
            label = f"{label} ({person.email})"
        items.append(
            {
                "id": person.id,
                "type": "person",
                "label": label,
                "ref": f"person:{person.id}",
            }
        )
    for org in organizations:
        label = org.name
        if org.domain:
            label = f"{label} ({org.domain})"
        items.append(
            {
                "id": org.id,
                "type": "organization",
                "label": label,
                "ref": f"organization:{org.id}",
            }
        )
    items.sort(key=lambda item: item["label"].lower())
    return items[:limit]


def search_response(db: Session, query: str, limit: int = 20) -> dict:
    items = search(db, query, limit)
    return list_response(items, limit, 0)
