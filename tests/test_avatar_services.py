"""Subscriber avatar durability and public-read contracts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile

from app.api.auth_flow import upload_avatar as upload_avatar_route
from app.config import settings
from app.main import app
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber
from app.services import avatar, user_profile
from app.services.file_storage import FileValidationError, file_uploads
from app.services.object_storage import StreamResult
from app.services.owner_commands import CommandContext
from app.web.public.avatars import public_subscriber_avatar

_PNG = b"\x89PNG\r\n\x1a\n" + b"valid-image-payload"


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("avatar_max_size_bytes", 2 * 1024 * 1024 + 1),
        ("avatar_allowed_types", "image/png,image/svg+xml"),
        ("avatar_url_prefix", "/other/avatars"),
    ),
)
def test_startup_refuses_unsupported_avatar_overrides(
    monkeypatch, setting_name: str, value: str | int
) -> None:
    baseline = replace(
        settings,
        avatar_max_size_bytes=2 * 1024 * 1024,
        avatar_allowed_types="image/jpeg,image/png,image/gif,image/webp",
        avatar_url_prefix="/static/avatars",
    )
    monkeypatch.setattr(avatar, "settings", replace(baseline, **{setting_name: value}))
    with pytest.raises(RuntimeError, match="AVATAR_"):
        avatar.require_compatible_avatar_policy()


def test_avatar_signature_check_is_prefix_only() -> None:
    config = file_uploads.get_domain_config("avatars")
    with pytest.raises(FileValidationError):
        file_uploads.validate(
            config=config,
            filename="photo.png",
            content_type="image/png",
            data=b"not-a-png",
        )
    # Current contract checks signature bytes, not a complete image decode.
    name, mime = file_uploads.validate(
        config=config,
        filename="photo.png",
        content_type="image/png",
        data=b"\x89PNG\r\n\x1a\nmalformed-image-body",
    )
    assert (name, mime) == ("photo.png", "image/png")


def test_startup_accepts_supported_stricter_avatar_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        avatar,
        "settings",
        replace(
            settings,
            avatar_max_size_bytes=1024 * 1024,
            avatar_allowed_types="image/png",
            avatar_url_prefix="/static/avatars",
        ),
    )
    avatar.require_compatible_avatar_policy()


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload(self, key: str, data: bytes, content_type: str | None) -> None:
        assert content_type == "image/png"
        self.objects[key] = data

    def exists(self, key: str) -> bool:
        return key in self.objects

    def stream(self, key: str) -> StreamResult:
        data = self.objects[key]
        return StreamResult(
            chunks=iter((data,)),
            content_type="image/png",
            content_length=len(data),
        )

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


def _subscriber(db_session, avatar_url: str | None = None) -> Subscriber:
    subscriber = Subscriber(
        first_name="Avatar",
        last_name="Owner",
        email=f"avatar-{uuid4()}@example.test",
        avatar_url=avatar_url,
    )
    db_session.add(subscriber)
    db_session.flush()
    # Keep the loaded id available without starting a read transaction after
    # commit; owner commands require a transaction-free Session at entry.
    db_session.expunge(subscriber)
    db_session.commit()
    return subscriber


def _upload_command(subscriber_id: UUID) -> avatar.UploadAvatarCommand:
    return avatar.UploadAvatarCommand(
        context=CommandContext.system(
            actor=str(subscriber_id), scope="subscriber", reason="avatar test"
        ),
        subscriber_id=subscriber_id,
        filename="photo.png",
        content_type="image/png",
        data=_PNG,
    )


def test_upload_uses_s3_and_selects_metadata_in_one_owner_command(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session, "/static/avatars/old.png")

    outcome = avatar.upload_avatar(db_session, _upload_command(subscriber.id))

    file_id = avatar.file_id_from_avatar_url(outcome.avatar_url)
    assert file_id is not None
    record = db_session.get(StoredFile, file_id)
    assert record is not None
    assert record.storage_provider == "s3"
    assert record.entity_type == "subscriber_avatar"
    assert record.entity_id == str(subscriber.id)
    assert record.owner_subscriber_id == subscriber.id
    assert record.storage_key_or_relative_path in storage.objects
    assert db_session.get(Subscriber, subscriber.id).avatar_url == outcome.avatar_url


def test_authenticated_upload_reads_only_size_limit_plus_one(monkeypatch) -> None:
    subscriber_id = uuid4()
    monkeypatch.setattr(
        user_profile, "settings", replace(settings, avatar_max_size_bytes=10)
    )
    file = MagicMock(spec=UploadFile)
    file.read = AsyncMock(return_value=b"x" * 11)

    def unexpected_write(*args, **kwargs):
        raise AssertionError("oversized input must never reach S3")

    monkeypatch.setattr(avatar, "upload_avatar", unexpected_write)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            user_profile.upload_avatar(MagicMock(), subscriber_id, subscriber_id, file)
        )
    assert error.value.status_code == 400
    file.read.assert_awaited_once_with(11)


def test_authenticated_upload_carries_principal_attribution(monkeypatch) -> None:
    subscriber_id = uuid4()
    monkeypatch.setattr(
        user_profile, "settings", replace(settings, avatar_max_size_bytes=100)
    )
    file = MagicMock(spec=UploadFile)
    file.filename = "photo.png"
    file.content_type = "image/png"
    file.read = AsyncMock(return_value=_PNG)
    captured: list[avatar.UploadAvatarCommand] = []

    def capture(db, command):
        captured.append(command)
        return avatar.AvatarOutcome(avatar_url=avatar.avatar_url_for_file(uuid4()))

    monkeypatch.setattr(avatar, "upload_avatar", capture)
    asyncio.run(
        user_profile.upload_avatar(MagicMock(), subscriber_id, subscriber_id, file)
    )
    assert captured[0].uploaded_by == subscriber_id
    assert captured[0].context.actor == f"subscriber:{subscriber_id}"


def test_api_key_cannot_change_subscriber_avatar() -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            upload_avatar_route(
                file=MagicMock(spec=UploadFile),
                auth={
                    "principal_type": "api_key",
                    "principal_id": str(uuid4()),
                    "subscriber_id": str(uuid4()),
                },
                db=MagicMock(),
            )
        )
    assert error.value.status_code == 403


def test_failed_stage_rolls_back_selection_and_metadata(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    old_url = "/static/avatars/old.png"
    subscriber = _subscriber(db_session, old_url)
    stage = file_uploads.stage_prepared_upload

    def fail_after_stage(*, db, prepared):
        stage(db=db, prepared=prepared)
        raise RuntimeError("later command failure")

    monkeypatch.setattr(file_uploads, "stage_prepared_upload", fail_after_stage)
    with pytest.raises(RuntimeError, match="later command failure"):
        avatar.upload_avatar(db_session, _upload_command(subscriber.id))

    db_session.expire_all()
    assert db_session.get(Subscriber, subscriber.id).avatar_url == old_url
    assert (
        db_session.query(StoredFile)
        .filter(StoredFile.entity_type == "subscriber_avatar")
        .count()
        == 0
    )
    assert storage.objects  # Orphan may remain for later reconciliation.


def test_replace_and_remove_keep_physical_objects(db_session, monkeypatch) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session)
    first = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    first_id = avatar.file_id_from_avatar_url(first.avatar_url)
    assert first_id is not None

    second = avatar.upload_avatar(
        db_session, replace(_upload_command(subscriber.id), data=_PNG + b"new")
    )
    assert second.avatar_url != first.avatar_url
    assert db_session.get(StoredFile, first_id).is_deleted is True
    assert avatar.resolve_public_avatar(db_session, first_id) is None
    assert storage.deleted == []
    assert len(storage.objects) == 2
    db_session.rollback()  # Close the assertion's read transaction.

    avatar.remove_avatar(
        db_session,
        avatar.RemoveAvatarCommand(
            context=CommandContext.system(
                actor=str(subscriber.id), scope="subscriber", reason="avatar removal"
            ),
            subscriber_id=subscriber.id,
        ),
    )
    assert db_session.get(Subscriber, subscriber.id).avatar_url is None
    assert storage.deleted == []


def test_failed_replacement_restores_old_selection_and_metadata(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session)
    first = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    first_id = avatar.file_id_from_avatar_url(first.avatar_url)
    assert first_id is not None

    def fail_after_change(db, changed_subscriber, context) -> None:
        raise RuntimeError("event stage failure")

    monkeypatch.setattr(avatar, "_stage_avatar_changed", fail_after_change)
    with pytest.raises(RuntimeError, match="event stage failure"):
        avatar.upload_avatar(
            db_session, replace(_upload_command(subscriber.id), data=_PNG + b"new")
        )

    db_session.expire_all()
    assert db_session.get(Subscriber, subscriber.id).avatar_url == first.avatar_url
    assert db_session.get(StoredFile, first_id).is_deleted is False
    assert avatar.resolve_public_avatar(db_session, first_id) is not None
    assert storage.deleted == []


def test_same_bytes_share_key_without_physical_cleanup(db_session, monkeypatch) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session)
    first = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    second = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    first_id = avatar.file_id_from_avatar_url(first.avatar_url)
    second_id = avatar.file_id_from_avatar_url(second.avatar_url)
    assert first_id is not None and second_id is not None and first_id != second_id
    first_record = db_session.get(StoredFile, first_id)
    second_record = db_session.get(StoredFile, second_id)
    assert (
        first_record.storage_key_or_relative_path
        == second_record.storage_key_or_relative_path
    )
    assert first_record.is_deleted is True
    assert second_record.is_deleted is False
    assert len(storage.objects) == 1
    assert storage.deleted == []


def test_public_reader_requires_current_owner_type_and_s3(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session)
    outcome = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    file_id = avatar.file_id_from_avatar_url(outcome.avatar_url)
    assert file_id is not None
    assert avatar.resolve_public_avatar(db_session, file_id) is not None
    response = public_subscriber_avatar(file_id, db_session)
    assert response.media_type == "image/png"

    record = db_session.get(StoredFile, file_id)
    record.entity_type = "branding_asset"
    db_session.flush()
    assert avatar.resolve_public_avatar(db_session, file_id) is None
    record.entity_type = "subscriber_avatar"
    record.entity_id = str(uuid4())
    db_session.flush()
    assert avatar.resolve_public_avatar(db_session, file_id) is None
    record.entity_id = str(subscriber.id)
    record.storage_provider = "local"
    db_session.flush()
    assert avatar.resolve_public_avatar(db_session, file_id) is None
    record.storage_provider = "s3"
    record.content_type = "image/jpeg"
    db_session.flush()
    assert avatar.resolve_public_avatar(db_session, file_id) is None


def test_public_reader_releases_transaction_before_slow_s3_stream(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session)
    outcome = avatar.upload_avatar(db_session, _upload_command(subscriber.id))
    file_id = avatar.file_id_from_avatar_url(outcome.avatar_url)
    assert file_id is not None

    def slow_stream(record: StoredFile) -> StreamResult:
        assert not db_session.in_transaction()  # Before S3 GetObject.

        def chunks():
            assert not db_session.in_transaction()
            yield b"first"
            time.sleep(0.05)
            assert (
                not db_session.in_transaction()
            )  # Slow client still holds no read txn.
            yield b"second"

        return StreamResult(
            chunks=chunks(), content_type="image/png", content_length=11
        )

    monkeypatch.setattr(file_uploads, "stream_file", slow_stream)
    response = public_subscriber_avatar(file_id, db_session)

    async def read_response() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(read_response()) == b"firstsecond"
    assert not db_session.in_transaction()


def test_legacy_url_is_not_managed_or_deleted(db_session, monkeypatch) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    legacy_url = "/static/avatars/legacy.png"
    subscriber = _subscriber(db_session, legacy_url)

    assert avatar.file_id_from_avatar_url(legacy_url) is None
    avatar.remove_avatar(
        db_session,
        avatar.RemoveAvatarCommand(
            context=CommandContext.system(
                actor=str(subscriber.id), scope="subscriber", reason="avatar removal"
            ),
            subscriber_id=subscriber.id,
        ),
    )
    assert storage.deleted == []
    assert db_session.get(Subscriber, subscriber.id).avatar_url is None


def test_legacy_static_url_mount_remains() -> None:
    assert any(route.path == "/static" for route in app.routes)


def test_migration_compare_and_swap_refuses_changed_url(
    db_session, monkeypatch
) -> None:
    storage = _Storage()
    monkeypatch.setattr(file_uploads, "storage", storage)
    subscriber = _subscriber(db_session, "/static/avatars/newer.png")
    command = replace(
        _upload_command(subscriber.id),
        expected_legacy_url="/static/avatars/older.png",
    )

    with pytest.raises(avatar.AvatarError) as error:
        avatar.upload_avatar(db_session, command)
    assert error.value.code == "customer.avatar.stale_legacy_url"
    assert db_session.get(Subscriber, subscriber.id).avatar_url.endswith("newer.png")
    assert (
        db_session.query(StoredFile)
        .filter(StoredFile.entity_type == "subscriber_avatar")
        .count()
        == 0
    )
