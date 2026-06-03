"""Service helpers for admin dashboard What's New items."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models.admin_whats_new import AdminWhatsNewItem

VISIBLE_STATUSES = {"featured", "active"}
ALL_STATUSES = ("draft", "featured", "active", "inactive", "archived")


def _normalize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_form_values(form: Mapping[str, Any]) -> dict[str, Any]:
    starts_at_raw = str(form.get("starts_at") or "").strip()
    ends_at_raw = str(form.get("ends_at") or "").strip()
    try:
        starts_at = _normalize_datetime(starts_at_raw)
    except ValueError as exc:
        raise ValueError("Start date must be a valid ISO date/time.") from exc
    try:
        ends_at = _normalize_datetime(ends_at_raw)
    except ValueError as exc:
        raise ValueError("End date must be a valid ISO date/time.") from exc
    return {
        "title": str(form.get("title") or "").strip(),
        "message": str(form.get("message") or "").strip(),
        "benefit_one": str(form.get("benefit_one") or "").strip(),
        "benefit_two": str(form.get("benefit_two") or "").strip(),
        "benefit_three": str(form.get("benefit_three") or "").strip(),
        "button_text": str(form.get("button_text") or "").strip(),
        "button_link": str(form.get("button_link") or "").strip(),
        "status": str(form.get("status") or "draft").strip().lower(),
        "starts_at": starts_at,
        "ends_at": ends_at,
    }


def validate_values(values: dict[str, Any]) -> str | None:
    if not values["title"]:
        return "Title is required."
    if not values["message"]:
        return "Short message is required."
    if not values["button_text"]:
        return "Button text is required."
    if not values["button_link"]:
        return "Button link is required."
    if values["status"] not in ALL_STATUSES:
        return "Status is invalid."
    if (
        values["starts_at"]
        and values["ends_at"]
        and values["starts_at"] > values["ends_at"]
    ):
        return "Start date must be before end date."
    return None


def list_items(db: Session, *, status: str | None = None) -> list[AdminWhatsNewItem]:
    query = db.query(AdminWhatsNewItem)
    if status and status in ALL_STATUSES:
        query = query.filter(AdminWhatsNewItem.status == status)
    status_rank = case((AdminWhatsNewItem.status == "featured", 0), else_=1)
    return query.order_by(
        status_rank.asc(),
        AdminWhatsNewItem.created_at.desc(),
        AdminWhatsNewItem.updated_at.desc(),
    ).all()


def get_item(db: Session, item_id: str) -> AdminWhatsNewItem | None:
    return db.get(AdminWhatsNewItem, item_id)


def create_item(db: Session, values: dict[str, Any]) -> AdminWhatsNewItem:
    item = AdminWhatsNewItem(
        title=values["title"],
        message=values["message"],
        benefit_one=values["benefit_one"] or None,
        benefit_two=values["benefit_two"] or None,
        benefit_three=values["benefit_three"] or None,
        button_text=values["button_text"],
        button_link=values["button_link"],
        status=values["status"],
        starts_at=values["starts_at"],
        ends_at=values["ends_at"],
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session, item: AdminWhatsNewItem, values: dict[str, Any]
) -> AdminWhatsNewItem:
    item.title = values["title"]
    item.message = values["message"]
    item.benefit_one = values["benefit_one"] or None
    item.benefit_two = values["benefit_two"] or None
    item.benefit_three = values["benefit_three"] or None
    item.button_text = values["button_text"]
    item.button_link = values["button_link"]
    item.status = values["status"]
    item.starts_at = values["starts_at"]
    item.ends_at = values["ends_at"]
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def set_status(db: Session, item: AdminWhatsNewItem, status: str) -> AdminWhatsNewItem:
    if status not in ALL_STATUSES:
        raise ValueError("Invalid status.")
    item.status = status
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def get_stats(db: Session) -> dict[str, int]:
    items = list_items(db)
    stats = dict.fromkeys(ALL_STATUSES, 0)
    for item in items:
        stats[item.status] = stats.get(item.status, 0) + 1
    stats["total"] = len(items)
    return stats


def get_visible_items(
    db: Session, *, now: datetime | None = None, limit: int = 4
) -> list[AdminWhatsNewItem]:
    current_time = now or datetime.now(UTC)
    status_rank = case((AdminWhatsNewItem.status == "featured", 0), else_=1)
    return (
        db.query(AdminWhatsNewItem)
        .filter(AdminWhatsNewItem.status.in_(VISIBLE_STATUSES))
        .filter(
            (AdminWhatsNewItem.starts_at.is_(None))
            | (AdminWhatsNewItem.starts_at <= current_time)
        )
        .filter(
            (AdminWhatsNewItem.ends_at.is_(None))
            | (AdminWhatsNewItem.ends_at >= current_time)
        )
        .order_by(
            status_rank.asc(),
            AdminWhatsNewItem.created_at.desc(),
            AdminWhatsNewItem.updated_at.desc(),
        )
        .limit(limit)
        .all()
    )


def serialize_for_dashboard(items: list[AdminWhatsNewItem]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in items:
        benefits = [
            benefit
            for benefit in (item.benefit_one, item.benefit_two, item.benefit_three)
            if benefit
        ]
        serialized.append(
            {
                "id": str(item.id),
                "title": item.title,
                "message": item.message,
                "benefits": benefits,
                "button_text": item.button_text,
                "button_link": item.button_link,
                "status": item.status,
            }
        )
    return serialized
