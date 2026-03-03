from __future__ import annotations

from io import BytesIO

from app.models.legal import LegalDocumentType
from app.services import web_legal


class _FakeUploadFile:
    def __init__(self, *, content: bytes, content_type: str, filename: str = "doc.pdf") -> None:
        self.file = BytesIO(content)
        self.content_type = content_type
        self.filename = filename


def test_list_page_data_parses_filters_and_paginates(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _FakeLegalDocuments:
        def list(self, **kwargs):
            calls["list"] = kwargs
            return ["doc-a"]

        def get_list_stats(self, db, *, document_type, is_published):
            calls["stats"] = {
                "document_type": document_type,
                "is_published": is_published,
            }
            return {"total": 51}

    monkeypatch.setattr(web_legal.legal_service, "legal_documents", _FakeLegalDocuments())

    data = web_legal.list_page_data(
        db=object(),
        document_type="privacy_policy",
        is_published="true",
        page=2,
        per_page=25,
    )

    assert data["documents"] == ["doc-a"]
    assert data["total_pages"] == 3
    assert calls["list"]["offset"] == 25
    assert calls["list"]["document_type"] == LegalDocumentType.privacy_policy
    assert calls["stats"]["is_published"] is True


def test_build_document_payloads() -> None:
    create_payload = web_legal.build_document_create_payload(
        document_type="terms_of_service",
        title="Terms",
        slug="terms",
        version="1.0",
        summary="",
        content="Body",
        is_published="false",
        effective_date=None,
    )
    assert create_payload.document_type == LegalDocumentType.terms_of_service
    assert create_payload.summary is None
    assert create_payload.is_published is False

    update_payload = web_legal.build_document_update_payload(
        title="Updated",
        slug="terms",
        version="1.1",
        summary="Short",
        content="",
        is_current="true",
        is_published="true",
        effective_date=None,
    )
    assert update_payload.content is None
    assert update_payload.is_current is True
    assert update_payload.is_published is True


def test_read_and_validate_upload() -> None:
    file = _FakeUploadFile(content=b"hello", content_type="application/pdf")
    content, filename, mime_type = web_legal.read_and_validate_upload(file)

    assert content == b"hello"
    assert filename == "doc.pdf"
    assert mime_type == "application/pdf"


def test_read_and_validate_upload_rejects_invalid_type() -> None:
    file = _FakeUploadFile(content=b"x", content_type="application/zip")

    try:
        web_legal.read_and_validate_upload(file)
    except ValueError as exc:
        assert "File type not allowed" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_read_and_validate_upload_rejects_oversized() -> None:
    big = b"x" * (web_legal.MAX_UPLOAD_SIZE_BYTES + 1)
    file = _FakeUploadFile(content=big, content_type="application/pdf")

    try:
        web_legal.read_and_validate_upload(file)
    except ValueError as exc:
        assert "10MB" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
