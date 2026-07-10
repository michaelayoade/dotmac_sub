from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationLabel,
    InboxLabel,
    InboxReplyMacro,
)
from app.services.common import coerce_uuid

_ALLOWED_LABEL_COLORS = {
    "slate",
    "blue",
    "indigo",
    "violet",
    "emerald",
    "teal",
    "amber",
    "orange",
    "rose",
    "red",
}


class InboxOperationError(ValueError):
    pass


@dataclass(frozen=True)
class LabelOption:
    id: str
    name: str
    slug: str
    color: str
    usage_count: int = 0


@dataclass(frozen=True)
class MacroOption:
    id: str
    name: str
    description: str | None
    body_text: str
    visibility: str
    actions: list[dict[str, Any]]
    execution_count: int


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100] if slug else "label"


def _label_name(value: str | None) -> str:
    name = str(value or "").strip()
    if not name:
        raise InboxOperationError("Label name is required.")
    if len(name) > 80:
        raise InboxOperationError("Label name must be 80 characters or fewer.")
    return name


def _label_color(value: str | None) -> str:
    color = str(value or "").strip().lower()
    return color if color in _ALLOWED_LABEL_COLORS else "slate"


def list_labels(db: Session, *, active_only: bool = True) -> list[LabelOption]:
    query = db.query(InboxLabel)
    if active_only:
        query = query.filter(InboxLabel.is_active.is_(True))
    usage_rows = (
        db.query(
            InboxConversationLabel.label_id,
            func.count(InboxConversationLabel.id).label("usage_count"),
        )
        .filter(InboxConversationLabel.is_active.is_(True))
        .group_by(InboxConversationLabel.label_id)
        .all()
    )
    usage_by_label = {row.label_id: int(row.usage_count or 0) for row in usage_rows}
    return [
        LabelOption(
            id=str(label.id),
            name=label.name,
            slug=label.slug,
            color=label.color or "slate",
            usage_count=usage_by_label.get(label.id, 0),
        )
        for label in query.order_by(func.lower(InboxLabel.name).asc()).all()
    ]


def conversation_labels(
    db: Session,
    conversation_id: str | UUID,
) -> list[LabelOption]:
    rows = (
        db.query(InboxConversationLabel, InboxLabel)
        .join(InboxLabel, InboxLabel.id == InboxConversationLabel.label_id)
        .filter(InboxConversationLabel.conversation_id == coerce_uuid(conversation_id))
        .filter(InboxConversationLabel.is_active.is_(True))
        .filter(InboxLabel.is_active.is_(True))
        .order_by(func.lower(InboxLabel.name).asc())
        .all()
    )
    return [
        LabelOption(
            id=str(label.id),
            name=label.name,
            slug=label.slug,
            color=label.color or "slate",
        )
        for _link, label in rows
    ]


def create_or_reactivate_label(
    db: Session,
    *,
    name: str,
    color: str | None = None,
) -> InboxLabel:
    normalized_name = _label_name(name)
    label = (
        db.query(InboxLabel)
        .filter(func.lower(InboxLabel.name) == normalized_name.lower())
        .first()
    )
    if label is None:
        label = InboxLabel(
            name=normalized_name,
            slug=_slugify(normalized_name),
            color=_label_color(color),
            is_active=True,
        )
        db.add(label)
    else:
        label.name = normalized_name
        label.slug = _slugify(normalized_name)
        label.color = _label_color(color)
        label.is_active = True
    db.flush()
    return label


def apply_label(
    db: Session,
    *,
    conversation: InboxConversation,
    label_id: str | UUID,
    applied_by_person_id: str | UUID | None = None,
) -> InboxConversationLabel:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None or not label.is_active:
        raise InboxOperationError("Label not found.")
    existing = (
        db.query(InboxConversationLabel)
        .filter(InboxConversationLabel.conversation_id == conversation.id)
        .filter(InboxConversationLabel.label_id == label.id)
        .order_by(InboxConversationLabel.created_at.desc())
        .first()
    )
    if existing is not None:
        existing.is_active = True
        existing.applied_by_person_id = coerce_uuid(applied_by_person_id)
        db.flush()
        return existing
    link = InboxConversationLabel(
        conversation_id=conversation.id,
        label_id=label.id,
        applied_by_person_id=coerce_uuid(applied_by_person_id),
        is_active=True,
    )
    db.add(link)
    db.flush()
    return link


def remove_label(
    db: Session,
    *,
    conversation: InboxConversation,
    label_id: str | UUID,
) -> None:
    link = (
        db.query(InboxConversationLabel)
        .filter(InboxConversationLabel.conversation_id == conversation.id)
        .filter(InboxConversationLabel.label_id == coerce_uuid(label_id))
        .filter(InboxConversationLabel.is_active.is_(True))
        .first()
    )
    if link is not None:
        link.is_active = False
        db.flush()


def update_label(
    db: Session,
    *,
    label_id: str | UUID,
    name: str,
    color: str | None = None,
    is_active: bool = True,
) -> InboxLabel:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None:
        raise InboxOperationError("Label not found.")
    normalized_name = _label_name(name)
    duplicate = (
        db.query(InboxLabel)
        .filter(func.lower(InboxLabel.name) == normalized_name.lower())
        .filter(InboxLabel.id != label.id)
        .first()
    )
    if duplicate is not None:
        raise InboxOperationError("A label with that name already exists.")
    label.name = normalized_name
    label.slug = _slugify(normalized_name)
    label.color = _label_color(color)
    label.is_active = bool(is_active)
    db.flush()
    return label


def delete_label(db: Session, *, label_id: str | UUID) -> None:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None:
        raise InboxOperationError("Label not found.")
    label.is_active = False
    db.flush()


def list_macros(
    db: Session,
    *,
    person_id: str | UUID | None = None,
    active_only: bool = True,
) -> list[MacroOption]:
    person_uuid = coerce_uuid(person_id)
    query = db.query(InboxReplyMacro)
    if active_only:
        query = query.filter(InboxReplyMacro.is_active.is_(True))
    if person_uuid is not None:
        query = query.filter(
            or_(
                InboxReplyMacro.visibility == "shared",
                InboxReplyMacro.created_by_person_id == person_uuid,
            )
        )
    else:
        query = query.filter(InboxReplyMacro.visibility == "shared")
    return [
        MacroOption(
            id=str(macro.id),
            name=macro.name,
            description=macro.description,
            body_text=macro.body_text,
            visibility=macro.visibility,
            actions=[
                action for action in (macro.actions or []) if isinstance(action, dict)
            ],
            execution_count=macro.execution_count,
        )
        for macro in query.order_by(
            InboxReplyMacro.execution_count.desc(),
            func.lower(InboxReplyMacro.name).asc(),
        )
        .limit(200)
        .all()
    ]


def create_macro(
    db: Session,
    *,
    name: str,
    body_text: str,
    description: str | None = None,
    visibility: str = "shared",
    actions: list[dict[str, Any]] | None = None,
    created_by_person_id: str | UUID | None = None,
) -> InboxReplyMacro:
    clean_name = str(name or "").strip()
    clean_body = str(body_text or "").strip()
    clean_visibility = str(visibility or "shared").strip().lower()
    if clean_visibility not in {"shared", "personal"}:
        clean_visibility = "shared"
    if not clean_name:
        raise InboxOperationError("Macro name is required.")
    if not clean_body:
        raise InboxOperationError("Macro body is required.")
    clean_actions = _validate_macro_actions(
        actions or [{"action_type": "reply_text", "params": {"body_text": clean_body}}]
    )
    macro = InboxReplyMacro(
        name=clean_name[:120],
        description=str(description or "").strip()[:1000] or None,
        body_text=clean_body,
        visibility=clean_visibility,
        actions=clean_actions,
        created_by_person_id=coerce_uuid(created_by_person_id),
        is_active=True,
    )
    db.add(macro)
    db.flush()
    return macro


def _validate_macro_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_types = {
        "assign_conversation",
        "set_status",
        "add_tag",
        "reply_text",
        "send_template",
    }
    cleaned: list[dict[str, Any]] = []
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            raise InboxOperationError(f"Invalid macro action at index {idx}.")
        action_type = str(action.get("action_type") or "").strip()
        if action_type not in valid_types:
            raise InboxOperationError(
                f"Invalid macro action type at index {idx}: {action_type}."
            )
        params = action.get("params")
        if not isinstance(params, dict):
            raise InboxOperationError(f"Macro action {action_type} requires params.")
        cleaned.append({"action_type": action_type, "params": dict(params)})
    return cleaned


def execute_macro_actions(
    db: Session,
    *,
    conversation: InboxConversation,
    macro_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    macro = record_macro_use(db, macro_id)
    if macro is None:
        raise InboxOperationError("Macro not found.")

    actor_uuid = coerce_uuid(actor_person_id)
    executed = 0
    failed = 0
    results: list[dict[str, object]] = []
    for action in macro.actions or []:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "").strip()
        raw_params = action.get("params")
        params: dict[str, Any] = (
            dict(raw_params) if isinstance(raw_params, dict) else {}
        )
        try:
            if action_type == "set_status":
                status_value = str(params.get("status") or "").strip().lower()
                if status_value not in {"open", "pending", "snoozed", "resolved"}:
                    raise InboxOperationError("Unsupported conversation status.")
                metadata = dict(conversation.metadata_ or {})
                history = metadata.get("status_history")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "from": conversation.status,
                        "to": status_value,
                        "at": _now_iso(),
                        "actor_id": str(actor_uuid) if actor_uuid else None,
                        "source": "team_inbox_macro",
                        "macro_id": str(macro.id),
                    }
                )
                metadata["status_history"] = history[-50:]
                conversation.status = status_value
                conversation.metadata_ = metadata
            elif action_type == "add_tag":
                label_name = str(params.get("tag") or params.get("label") or "").strip()
                if not label_name:
                    raise InboxOperationError("Label name is required.")
                label = create_or_reactivate_label(db, name=label_name)
                apply_label(
                    db,
                    conversation=conversation,
                    label_id=label.id,
                    applied_by_person_id=actor_person_id,
                )
            elif action_type == "assign_conversation":
                metadata = dict(conversation.metadata_ or {})
                macro_assignments = metadata.get("macro_assignments")
                if not isinstance(macro_assignments, list):
                    macro_assignments = []
                macro_assignments.append(
                    {
                        "person_id": params.get("person_id") or params.get("agent_id"),
                        "service_team_id": params.get("service_team_id")
                        or params.get("team_id"),
                        "actor_id": str(actor_uuid) if actor_uuid else None,
                        "at": _now_iso(),
                        "macro_id": str(macro.id),
                    }
                )
                metadata["macro_assignments"] = macro_assignments[-20:]
                conversation.metadata_ = metadata
            elif action_type in {"reply_text", "send_template"}:
                # Reply dispatch remains explicit in the admin route so provider
                # errors are visible to the agent before the conversation changes.
                pass
            else:
                raise InboxOperationError(f"Unsupported macro action: {action_type}.")
            executed += 1
            results.append({"action": action_type, "ok": True})
        except Exception as exc:
            failed += 1
            results.append({"action": action_type, "ok": False, "error": str(exc)})
    db.flush()
    return {
        "ok": failed == 0,
        "actions_executed": executed,
        "actions_failed": failed,
        "results": results,
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def record_macro_use(
    db: Session, macro_id: str | UUID | None
) -> InboxReplyMacro | None:
    if not macro_id:
        return None
    try:
        macro_uuid = coerce_uuid(macro_id)
    except (TypeError, ValueError):
        return None
    macro = db.get(InboxReplyMacro, macro_uuid)
    if macro is None or not macro.is_active:
        return None
    macro.execution_count = int(macro.execution_count or 0) + 1
    db.flush()
    return macro
