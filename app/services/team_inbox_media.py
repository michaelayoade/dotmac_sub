from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxMediaAsset, InboxMessage


def _text(value: object, *, max_length: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_length] if max_length is not None else text


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _asset_type(raw: dict[str, Any]) -> str:
    for key in ("type", "asset_type", "media_type", "kind"):
        value = _text(raw.get(key), max_length=40)
        if value:
            return value
    mime_type = _text(raw.get("mime_type") or raw.get("mime"), max_length=160)
    if mime_type and "/" in mime_type:
        return mime_type.split("/", 1)[0][:40]
    return "attachment"


def _provider_media_id(raw: dict[str, Any]) -> str | None:
    for key in ("provider_media_id", "media_id", "attachment_id", "id"):
        value = _text(raw.get(key), max_length=255)
        if value:
            return value
    return None


def _file_name(raw: dict[str, Any]) -> str | None:
    return _text(raw.get("file_name") or raw.get("filename"), max_length=255)


def _source_url(raw: dict[str, Any]) -> str | None:
    return _text(raw.get("url") or raw.get("source_url") or raw.get("link"))


def _existing_asset(
    db: Session,
    *,
    message: InboxMessage,
    raw: dict[str, Any],
) -> InboxMediaAsset | None:
    provider = _text(raw.get("provider"), max_length=80)
    provider_media_id = _provider_media_id(raw)
    if provider_media_id:
        existing = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .filter(InboxMediaAsset.provider_media_id == provider_media_id)
            .first()
        )
        if existing is not None:
            return existing
    file_name = _file_name(raw)
    source_url = _source_url(raw)
    if not provider and not provider_media_id and not file_name and not source_url:
        return None
    return (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.message_id == message.id)
        .filter(InboxMediaAsset.provider == provider)
        .filter(InboxMediaAsset.provider_media_id == provider_media_id)
        .filter(InboxMediaAsset.file_name == file_name)
        .filter(InboxMediaAsset.source_url == source_url)
        .first()
    )


def promote_message_attachments(
    db: Session,
    *,
    message: InboxMessage,
    provider: str | None = None,
) -> list[InboxMediaAsset]:
    metadata = message.metadata_ or {}
    raw_items = metadata.get("attachments")
    if not isinstance(raw_items, list):
        return []

    assets: list[InboxMediaAsset] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        raw = dict(raw_item)
        if provider and not raw.get("provider"):
            raw["provider"] = provider
        existing = _existing_asset(db, message=message, raw=raw)
        if existing is not None:
            assets.append(existing)
            continue
        asset = InboxMediaAsset(
            conversation_id=message.conversation_id,
            message_id=message.id,
            channel_type=message.channel_type,
            direction=message.direction,
            provider=_text(raw.get("provider"), max_length=80),
            provider_media_id=_provider_media_id(raw),
            asset_type=_asset_type(raw),
            file_name=_file_name(raw),
            mime_type=_text(raw.get("mime_type") or raw.get("mime"), max_length=160),
            file_size=_int(raw.get("file_size") or raw.get("size")),
            caption=_text(raw.get("caption")),
            source_url=_source_url(raw),
            storage_url=_text(raw.get("storage_url")),
            checksum_sha256=_text(raw.get("checksum_sha256"), max_length=64),
            download_status=_text(raw.get("download_status"), max_length=40)
            or ("stored" if raw.get("storage_url") else "metadata_only"),
            metadata_=raw,
        )
        db.add(asset)
        assets.append(asset)
    db.flush()
    return assets


def promote_unmaterialized_assets(
    db: Session,
    *,
    limit: int = 200,
) -> int:
    rows = (
        db.query(InboxMessage)
        .order_by(InboxMessage.created_at.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    created_or_existing = 0
    for message in rows:
        metadata = message.metadata_ or {}
        if not isinstance(metadata.get("attachments"), list):
            continue
        before_count = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .count()
        )
        assets = promote_message_attachments(db, message=message)
        after_count = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.message_id == message.id)
            .count()
        )
        if assets and after_count >= before_count:
            created_or_existing += len(assets)
    return created_or_existing


def assets_for_messages(
    db: Session,
    message_ids: list[UUID],
) -> dict[UUID, list[InboxMediaAsset]]:
    if not message_ids:
        return {}
    rows = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.message_id.in_(message_ids))
        .order_by(InboxMediaAsset.created_at.asc())
        .all()
    )
    grouped: dict[UUID, list[InboxMediaAsset]] = {}
    for row in rows:
        if row.message_id is not None:
            grouped.setdefault(row.message_id, []).append(row)
    return grouped
