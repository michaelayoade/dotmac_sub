"""Synthetic portraits prove pixel orientation and metadata-free disclosure."""

from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, ImageOps, PngImagePlugin

from app.services import field_identity as owner
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult
from tests.test_field_identity import (  # noqa: F401
    actors as _identity_actors,
)
from tests.test_field_identity import (
    command,
    review_command,
    upload,
)
from tests.test_field_identity import (
    prepared_object_storage as prepared_object_storage,
)


@pytest.fixture
def actors(db_session):
    return _identity_actors.__wrapped__(db_session)


def _portrait(format):
    image = Image.new("RGB", (3, 2))
    image.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (0, 255, 255),
            (255, 0, 255),
        ]
    )
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "synthetic-private-location-and-owner"
    exif[34853] = {1: "N", 2: (10.0, 20.0, 30.0)}
    options = {"exif": exif, "icc_profile": b"synthetic-private-color-profile"}
    if format == "PNG":
        text = PngImagePlugin.PngInfo()
        text.add_text("GPSLatitude", "synthetic-private-location-and-owner")
        text.add_text("XML:com.adobe.xmp", "synthetic-private-xmp")
        options["pnginfo"] = text
    if format == "WEBP":
        options["xmp"] = b"synthetic-private-xmp"
    buffer = BytesIO()
    image.save(buffer, format=format, **options)
    return buffer.getvalue() + b"synthetic-private-trailing-payload"


def _assert_clean_pixels(original, cleaned):
    with Image.open(BytesIO(original)) as source:
        expected = ImageOps.exif_transpose(source).convert("RGB")
        with Image.open(BytesIO(cleaned)) as result:
            assert result.format == "PNG"
            assert result.size == expected.size == (2, 3)
            assert result.convert("RGB").tobytes() == expected.tobytes()
            assert not result.getexif()
            assert result.info == {}
    assert b"synthetic-private" not in cleaned


@pytest.mark.parametrize(
    "format,mime",
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_upload_stores_sanitized_oriented_portrait_and_evidence_stays_original(
    monkeypatch, format, mime
):
    subject_id = uuid4()
    source = _portrait(format)
    prepared_bytes = []

    def prepare(**kwargs):
        prepared_bytes.append(kwargs["data"])
        return replace(
            upload(subject_id, kwargs["entity_type"] == "field_identity_portrait"),
            original_filename=kwargs["original_filename"],
            content_type=kwargs["content_type"],
            file_size=len(kwargs["data"]),
        )

    monkeypatch.setattr(file_uploads, "prepare_upload", prepare)
    portrait = owner.prepare_identity_upload(
        subject_id=subject_id,
        portrait=True,
        filename="private-person-name.jpg",
        content_type=mime,
        data=source,
    )
    assert portrait.content_type == "image/png"
    assert portrait.original_filename == "portrait.png"
    _assert_clean_pixels(source, prepared_bytes[0])
    owner.prepare_identity_upload(
        subject_id=subject_id,
        portrait=False,
        filename="evidence.jpg",
        content_type=mime,
        data=source,
    )
    assert prepared_bytes[1] == source


@pytest.mark.parametrize(
    "format,mime",
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_existing_reviewed_original_stream_is_sanitized_without_new_approval(
    db_session, actors, monkeypatch, format, mime
):
    subject, reviewer, _ = actors
    owner.submit(
        db_session,
        command=replace(
            command(subject), portrait=replace(upload(subject), content_type=mime)
        ),
    )
    approved = owner.review(db_session, command=review_command(subject, reviewer))
    source = _portrait(format)
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda file: StreamResult(
            chunks=iter((source[:20], source[20:])),
            content_type=mime,
            content_length=len(source),
        ),
    )
    stream = owner.approved_portrait_stream(
        db_session, subject_id=subject, revision=approved.revision
    )
    cleaned = b"".join(stream.chunks)
    assert stream.content_type == "image/png"
    assert stream.content_length == len(cleaned)
    _assert_clean_pixels(source, cleaned)
    assert (
        owner.approved_customer_card(db_session, subject_id=subject).revision
        == approved.revision
    )


@pytest.mark.parametrize(
    "cap", ["MAX_PORTRAIT_PIXELS", "MAX_PORTRAIT_DIMENSION", "MAX_IDENTITY_FILE_BYTES"]
)
def test_portrait_resource_caps_fail_closed(monkeypatch, cap):
    monkeypatch.setattr(owner, cap, 1)
    with pytest.raises(owner.FieldIdentityError):
        owner.normalize_identity_portrait(
            data=_portrait("PNG"), content_type="image/png"
        )


def test_invalid_legacy_bytes_never_become_customer_stream(
    db_session, actors, monkeypatch
):
    subject, reviewer, _ = actors
    owner.submit(db_session, command=command(subject))
    owner.review(db_session, command=review_command(subject, reviewer))
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda file: StreamResult(
            chunks=iter((b"invalid",)), content_type="image/png", content_length=7
        ),
    )
    with pytest.raises(owner.FieldIdentityError):
        owner.approved_portrait_stream(db_session, subject_id=subject, revision=1)


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations_preserve_displayed_pixels(orientation):
    with Image.open(BytesIO(_portrait("PNG"))) as source:
        exif = source.getexif()
        exif[274] = orientation
        encoded = BytesIO()
        source.save(encoded, format="PNG", exif=exif)
    original = encoded.getvalue()
    clean = owner.normalize_identity_portrait(data=original, content_type="image/png")
    with (
        Image.open(BytesIO(original)) as source,
        Image.open(BytesIO(clean.data)) as normalized,
    ):
        expected = ImageOps.exif_transpose(source).convert("RGB")
        assert normalized.size == expected.size
        assert normalized.tobytes() == expected.tobytes()
        assert not normalized.getexif()


def test_truncated_animated_and_decompression_bomb_portraits_fail_closed(monkeypatch):
    with pytest.raises(owner.FieldIdentityError):
        owner.normalize_identity_portrait(
            data=_portrait("PNG")[:40], content_type="image/png"
        )
    animated = BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        animated,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
    )
    with pytest.raises(owner.FieldIdentityError):
        owner.normalize_identity_portrait(
            data=animated.getvalue(), content_type="image/png"
        )
    original = _portrait("PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(owner.FieldIdentityError):
        owner.normalize_identity_portrait(data=original, content_type="image/png")


@pytest.mark.parametrize("declared_length", [None, 17])
def test_oversized_legacy_stream_is_bounded_and_closed(
    db_session, actors, monkeypatch, declared_length
):
    subject, reviewer, _ = actors
    owner.submit(db_session, command=command(subject))
    owner.review(db_session, command=review_command(subject, reviewer))

    class Chunks:
        closed = False
        reads = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.reads += 1
            return b"x" * 17

        def close(self):
            self.closed = True

    chunks = Chunks()
    monkeypatch.setattr(owner, "MAX_IDENTITY_FILE_BYTES", 16)
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda file: StreamResult(
            chunks=chunks, content_type="image/png", content_length=declared_length
        ),
    )
    with pytest.raises(owner.FieldIdentityError):
        owner.approved_portrait_stream(db_session, subject_id=subject, revision=1)
    assert chunks.closed
    assert chunks.reads == (1 if declared_length is None else 0)
