from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models.stored_file import StoredFile
from app.models.team_inbox import (
    InboxConversation,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult


class MediaUploadError(ValueError):
    """Attachment upload could not be staged for an Inbox conversation."""


class MediaContentError(LookupError):
    """Attachment content is not available to stream."""


@dataclass(frozen=True)
class StagedAttachmentInput:
    file_name: str
    content_type: str | None
    data: bytes
    uploaded_by: str | None = None


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


def media_content_url(asset_id: UUID | str) -> str:
    return f"/admin/inbox/media/{asset_id}/content"


def _stored_file_id(asset: InboxMediaAsset) -> UUID | None:
    metadata = asset.metadata_ if isinstance(asset.metadata_, dict) else {}
    value = metadata.get("stored_file_id")
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


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
            or (
                "stored"
                if raw.get("storage_url")
                else "remote_available"
                if _source_url(raw)
                else "metadata_only"
            ),
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


def stage_outbound_attachments(
    db: Session,
    *,
    conversation_id: UUID,
    files: Iterable[StagedAttachmentInput],
) -> list[InboxMediaAsset]:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        raise MediaUploadError("Conversation not found.")
    assets: list[InboxMediaAsset] = []
    for item in files:
        if not item.data:
            raise MediaUploadError("Attachment is empty.")
        stored = file_uploads.stage_upload(
            db=db,
            domain="attachments",
            entity_type="inbox_conversation",
            entity_id=str(conversation.id),
            original_filename=item.file_name,
            content_type=item.content_type,
            data=item.data,
            uploaded_by=item.uploaded_by,
            owner_subscriber_id=conversation.subscriber_id,
        )
        asset = InboxMediaAsset(
            conversation_id=conversation.id,
            message_id=None,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.outbound.value,
            provider="stored_file",
            provider_media_id=str(stored.id),
            asset_type=(stored.content_type or "attachment").split("/", 1)[0],
            file_name=stored.original_filename,
            mime_type=stored.content_type,
            file_size=stored.file_size,
            caption=None,
            source_url=None,
            storage_url=media_content_url(stored.id),
            checksum_sha256=stored.checksum,
            download_status="stored",
            metadata_={"stored_file_id": str(stored.id)},
        )
        db.add(asset)
        assets.append(asset)
    db.flush()
    return assets


def bind_assets_to_message(
    db: Session,
    *,
    conversation_id: UUID,
    message_id: UUID,
    asset_ids: Iterable[UUID],
) -> list[InboxMediaAsset]:
    clean_ids = list(dict.fromkeys(asset_ids))
    if not clean_ids:
        return []
    rows = (
        db.query(InboxMediaAsset)
        .filter(InboxMediaAsset.id.in_(clean_ids))
        .filter(InboxMediaAsset.conversation_id == conversation_id)
        .all()
    )
    if len(rows) != len(clean_ids):
        raise MediaUploadError("One or more attachments were not found.")
    for asset in rows:
        if asset.message_id is not None and asset.message_id != message_id:
            raise MediaUploadError("One or more attachments are already sent.")
        asset.message_id = message_id
    db.flush()
    return rows


def resolve_delivery_attachments(
    db: Session,
    *,
    asset_ids: Iterable[UUID | str],
) -> list[dict[str, object]]:
    clean_ids: list[UUID] = []
    for raw_id in asset_ids:
        try:
            clean_ids.append(raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id)))
        except (TypeError, ValueError):
            continue
    if not clean_ids:
        return []
    rows = db.query(InboxMediaAsset).filter(InboxMediaAsset.id.in_(clean_ids)).all()
    attachments: list[dict[str, object]] = []
    for asset in rows:
        attachments.append(
            {
                "id": str(asset.id),
                "type": asset.asset_type,
                "file_name": asset.file_name,
                "mime_type": asset.mime_type,
                "file_size": asset.file_size,
                "url": asset.source_url or asset.storage_url,
                "source_url": asset.source_url,
                "storage_url": asset.storage_url,
                "stored_file_id": str(_stored_file_id(asset) or ""),
            }
        )
    return attachments


def stream_asset_content(db: Session, *, asset_id: UUID) -> StreamResult:
    asset = db.get(InboxMediaAsset, asset_id)
    if asset is None:
        stored = db.get(StoredFile, asset_id)
        if stored is None or stored.is_deleted:
            raise MediaContentError("Attachment not found.")
        try:
            return file_uploads.stream_file(stored)
        except ObjectNotFoundError as exc:
            raise MediaContentError("Attachment content is missing.") from exc
    stored_file_id = _stored_file_id(asset)
    if stored_file_id is not None:
        stored = db.get(StoredFile, stored_file_id)
        if stored is None or stored.is_deleted:
            raise MediaContentError("Attachment content is missing.")
        try:
            return file_uploads.stream_file(stored)
        except ObjectNotFoundError as exc:
            raise MediaContentError("Attachment content is missing.") from exc
    if asset.source_url:
        try:
            response = httpx.get(asset.source_url, timeout=20.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MediaContentError(
                "Remote attachment content is unavailable."
            ) from exc
        content = response.content
        content_type = response.headers.get("content-type") or asset.mime_type
        return StreamResult(
            chunks=iter((content,)),
            content_type=content_type,
            content_length=len(content),
        )
    raise MediaContentError("Attachment content is unavailable.")
