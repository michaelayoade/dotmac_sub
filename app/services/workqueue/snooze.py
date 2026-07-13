"""Snooze CRUD for the workqueue.

SOT service-ownership contract: the API layer never commits. Writes have an
uncommitted core (``snooze_item`` / ``clear_snooze``) usable inside a larger
transaction, plus a ``*_committed`` entry point that owns the commit and is what
``app/api/workqueue.py`` calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.workqueue import WorkqueueItemKind, WorkqueueSnooze
from app.services.common import coerce_uuid
from app.services.workqueue.types import ItemKind


def _coerce_kind(item_kind: str | ItemKind) -> str:
    value = str(item_kind)
    try:
        WorkqueueItemKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown workqueue item kind: {value}"
        ) from exc
    return value


def snooze_item(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    user_uuid = coerce_uuid(user_id)
    item_uuid = coerce_uuid(item_id)
    kind = _coerce_kind(item_kind)
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == user_uuid)
        .filter(WorkqueueSnooze.item_kind == kind)
        .filter(WorkqueueSnooze.item_id == item_uuid)
        .one_or_none()
    )
    if snooze is None:
        snooze = WorkqueueSnooze(
            user_id=user_uuid,
            item_kind=kind,
            item_id=item_uuid,
        )
        db.add(snooze)
    snooze.snooze_until = snooze_until
    snooze.until_next_reply = until_next_reply
    db.flush()
    return snooze


def clear_snooze(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
) -> None:
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == coerce_uuid(user_id))
        .filter(WorkqueueSnooze.item_kind == _coerce_kind(item_kind))
        .filter(WorkqueueSnooze.item_id == coerce_uuid(item_id))
        .one_or_none()
    )
    if snooze is None:
        raise HTTPException(status_code=404, detail="Snooze not found")
    db.delete(snooze)
    db.flush()


def active_snoozed_ids(
    db: Session,
    *,
    user_id: str | UUID,
    now: datetime | None = None,
) -> dict[ItemKind, set[UUID]]:
    """Item ids the user has snoozed, grouped by kind.

    ``until_next_reply`` snoozes stay active until the conversation gets a new
    inbound message (cleared by ``release_until_next_reply``); a snooze with no
    ``snooze_until`` and no ``until_next_reply`` is treated as indefinite.
    """
    current_time = now or datetime.now(UTC)
    rows = (
        db.query(WorkqueueSnooze.item_kind, WorkqueueSnooze.item_id)
        .filter(WorkqueueSnooze.user_id == coerce_uuid(user_id))
        .filter(
            or_(
                WorkqueueSnooze.until_next_reply.is_(True),
                WorkqueueSnooze.snooze_until.is_(None),
                and_(
                    WorkqueueSnooze.snooze_until.isnot(None),
                    WorkqueueSnooze.snooze_until > current_time,
                ),
            )
        )
        .all()
    )
    snoozed: dict[ItemKind, set[UUID]] = {kind: set() for kind in ItemKind}
    for item_kind, item_id in rows:
        try:
            kind = ItemKind(item_kind)
        except ValueError:
            # A kind the aggregator no longer projects (e.g. a retired source).
            continue
        snoozed[kind].add(item_id)
    return snoozed


def release_until_next_reply(db: Session, *, conversation_id: str | UUID) -> list[UUID]:
    """Drop ``until_next_reply`` snoozes for a conversation that just replied.

    Returns the user ids whose queue changed (callers use it to target realtime
    updates). Uncommitted — the caller owns the transaction.
    """
    rows = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.item_kind == ItemKind.conversation.value)
        .filter(WorkqueueSnooze.item_id == coerce_uuid(conversation_id))
        .filter(WorkqueueSnooze.until_next_reply.is_(True))
        .all()
    )
    affected = [row.user_id for row in rows]
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()
    return affected


def _emit(user_id: UUID, item_kind: str, item_id: UUID, change: str) -> None:
    """Tell the owner's live queue that a snooze hid/restored an item."""
    from app.services.workqueue.events import emit_change

    emit_change(
        item_kind=item_kind,
        item_id=item_id,
        change=change,  # type: ignore[arg-type]
        affected_user_ids=[user_id],
    )


# --- Commit-owning entry points (called by the API layer) --------------------


def snooze_item_committed(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    snooze = snooze_item(
        db,
        user_id=user_id,
        item_kind=item_kind,
        item_id=item_id,
        snooze_until=snooze_until,
        until_next_reply=until_next_reply,
    )
    db.commit()
    db.refresh(snooze)
    _emit(snooze.user_id, snooze.item_kind, snooze.item_id, "removed")
    return snooze


def clear_snooze_committed(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
) -> None:
    clear_snooze(db, user_id=user_id, item_kind=item_kind, item_id=item_id)
    db.commit()
    _emit(
        coerce_uuid(user_id),
        _coerce_kind(item_kind),
        coerce_uuid(item_id),
        "added",
    )
