"""Shared helpers for OLT CRUD service modules."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.network import OltCard, OltCardPort, OltShelf

_CANONICAL_PON_NAME_RE = re.compile(r"^\d+/\d+/\d+$")


def canonical_pon_name_from_card_port(
    db: Session,
    card_port: OltCardPort,
) -> str:
    card = db.get(OltCard, card_port.card_id)
    shelf = db.get(OltShelf, card.shelf_id) if card else None
    if shelf and card:
        return f"{shelf.shelf_number}/{card.slot_number}/{card_port.port_number}"
    if getattr(card_port, "name", None):
        return str(card_port.name)
    return f"pon-{card_port.port_number}"


def parse_canonical_pon_name(name: str | None) -> tuple[str, int] | None:
    text = str(name or "").strip()
    if not _CANONICAL_PON_NAME_RE.fullmatch(text):
        return None
    board, port = text.rsplit("/", 1)
    return board, int(port)
