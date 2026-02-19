from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.subscriber import Organization, Subscriber
from app.services.response import list_response


def search(db: Session, query: str, limit: int = 20) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    people = (
        db.query(Subscriber)
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
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
    for subscriber in people:
        label = f"{subscriber.first_name} {subscriber.last_name}"
        if subscriber.email:
            label = f"{label} ({subscriber.email})"
        items.append(
            {
                "id": subscriber.id,
                # Backwards-compat: historically this search returned "person"
                # and other form helpers still parse refs like "person:<uuid>".
                "type": "person",
                "label": label,
                "ref": f"person:{subscriber.id}",
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
