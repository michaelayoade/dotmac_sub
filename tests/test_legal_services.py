"""Tests for legal document services."""

import os
import uuid

from app.models.legal import LegalDocument, LegalDocumentType
from app.models.stored_file import StoredFile
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate
from app.services import legal

# =============================================================================
# List Tests
# =============================================================================


class TestLegalDocumentList:
    """Tests for legal document listing."""

    def test_list_all(self, db_session):
        """Test listing all documents."""
        doc1 = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Terms of Service",
            slug="tos",
            version="1.0",
        )
        doc2 = LegalDocument(
            document_type=LegalDocumentType.privacy_policy,
            title="Privacy Policy",
            slug="privacy",
            version="1.0",
        )
        db_session.add_all([doc1, doc2])
        db_session.commit()

        results = legal.legal_documents.list(db_session)
        assert len(results) >= 2

    def test_list_by_document_type(self, db_session):
        """Test filtering by document type."""
        tos = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="TOS",
            slug="tos-filter",
            version="1.0",
        )
        privacy = LegalDocument(
            document_type=LegalDocumentType.privacy_policy,
            title="Privacy",
            slug="privacy-filter",
            version="1.0",
        )
        db_session.add_all([tos, privacy])
        db_session.commit()

        results = legal.legal_documents.list(
            db_session, document_type=LegalDocumentType.terms_of_service
        )
        assert all(d.document_type == LegalDocumentType.terms_of_service for d in results)

    def test_list_by_published(self, db_session):
        """Test filtering by published status."""
        published = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Published Doc",
            slug="published-doc",
            version="1.0",
            is_published=True,
        )
        draft = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Draft Doc",
            slug="draft-doc",
            version="1.0",
            is_published=False,
        )
        db_session.add_all([published, draft])
        db_session.commit()

        results = legal.legal_documents.list(db_session, is_published=True)
        assert all(d.is_published for d in results)

    def test_list_by_current(self, db_session):
        """Test filtering by current status."""
        current = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Current Doc",
            slug="current-doc",
            version="2.0",
            is_current=True,
        )
        old = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Old Doc",
            slug="old-doc",
            version="1.0",
            is_current=False,
        )
        db_session.add_all([current, old])
        db_session.commit()

        results = legal.legal_documents.list(db_session, is_current=True)
        assert all(d.is_current for d in results)

    def test_list_ordering(self, db_session):
        """Test ordering results."""
        doc1 = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="A Doc",
            slug="a-doc",
            version="1.0",
        )
        doc2 = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="B Doc",
            slug="b-doc",
            version="1.0",
        )
        db_session.add_all([doc1, doc2])
        db_session.commit()

        # Test ascending
        results = legal.legal_documents.list(
            db_session, order_by="title", order_dir="asc"
        )
        titles = [d.title for d in results]
        assert titles == sorted(titles)

    def test_list_pagination(self, db_session):
        """Test pagination."""
        for i in range(5):
            db_session.add(LegalDocument(
                document_type=LegalDocumentType.other,
                title=f"Doc {i}",
                slug=f"doc-page-{i}",
                version="1.0",
            ))
        db_session.commit()

        page1 = legal.legal_documents.list(db_session, limit=2, offset=0)
        page2 = legal.legal_documents.list(db_session, limit=2, offset=2)
        assert len(page1) <= 2
        assert len(page2) <= 2


# =============================================================================
# Get Tests
# =============================================================================


class TestLegalDocumentGet:
    """Tests for getting legal documents."""

    def test_get_by_id(self, db_session):
        """Test getting document by ID."""
        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Get Test",
            slug="get-test",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        result = legal.legal_documents.get(db_session, str(doc.id))
        assert result is not None
        assert result.id == doc.id

    def test_get_not_found(self, db_session):
        """Test getting non-existent document."""
        result = legal.legal_documents.get(db_session, str(uuid.uuid4()))
        assert result is None

    def test_get_by_slug(self, db_session):
        """Test getting document by slug."""
        doc = LegalDocument(
            document_type=LegalDocumentType.privacy_policy,
            title="Slug Test",
            slug="slug-test-doc",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        result = legal.legal_documents.get_by_slug(db_session, "slug-test-doc")
        assert result is not None
        assert result.slug == "slug-test-doc"

    def test_get_by_slug_not_found(self, db_session):
        """Test getting non-existent slug."""
        result = legal.legal_documents.get_by_slug(db_session, "nonexistent-slug")
        assert result is None

    def test_get_current_by_type(self, db_session):
        """Test getting current published document by type."""
        old_doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Old TOS",
            slug="old-tos",
            version="1.0",
            is_current=False,
            is_published=True,
        )
        current_doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Current TOS",
            slug="current-tos",
            version="2.0",
            is_current=True,
            is_published=True,
        )
        db_session.add_all([old_doc, current_doc])
        db_session.commit()

        result = legal.legal_documents.get_current_by_type(
            db_session, LegalDocumentType.terms_of_service
        )
        assert result is not None
        assert result.is_current is True
        assert result.is_published is True

    def test_get_current_by_type_not_published(self, db_session):
        """Test current but not published returns None."""
        doc = LegalDocument(
            document_type=LegalDocumentType.acceptable_use,
            title="Draft AUP",
            slug="draft-aup",
            version="1.0",
            is_current=True,
            is_published=False,
        )
        db_session.add(doc)
        db_session.commit()

        result = legal.legal_documents.get_current_by_type(
            db_session, LegalDocumentType.acceptable_use
        )
        assert result is None


# =============================================================================
# Create Tests
# =============================================================================


class TestLegalDocumentCreate:
    """Tests for creating legal documents."""

    def test_create_basic(self, db_session):
        """Test basic document creation."""
        payload = LegalDocumentCreate(
            document_type=LegalDocumentType.terms_of_service,
            title="Terms of Service",
            slug="tos-create",
            version="1.0",
        )

        result = legal.legal_documents.create(db_session, payload)

        assert result.id is not None
        assert result.title == "Terms of Service"
        assert result.slug == "tos-create"
        assert result.is_published is False

    def test_create_with_content(self, db_session):
        """Test creating document with content."""
        payload = LegalDocumentCreate(
            document_type=LegalDocumentType.privacy_policy,
            title="Privacy Policy",
            slug="privacy-create",
            version="1.0",
            summary="Our privacy practices",
            content="Full privacy policy text...",
        )

        result = legal.legal_documents.create(db_session, payload)

        assert result.summary == "Our privacy practices"
        assert result.content == "Full privacy policy text..."

    def test_create_published_sets_timestamp(self, db_session):
        """Test creating published document sets published_at."""
        payload = LegalDocumentCreate(
            document_type=LegalDocumentType.terms_of_service,
            title="Published TOS",
            slug="published-tos-create",
            version="1.0",
            is_published=True,
        )

        result = legal.legal_documents.create(db_session, payload)

        assert result.is_published is True
        assert result.published_at is not None
        assert result.is_current is True

    def test_create_published_makes_current(self, db_session):
        """Test publishing new document makes it current."""
        old_doc = LegalDocument(
            document_type=LegalDocumentType.cookie_policy,
            title="Old Cookie Policy",
            slug="old-cookie",
            version="1.0",
            is_current=True,
            is_published=True,
        )
        db_session.add(old_doc)
        db_session.commit()

        payload = LegalDocumentCreate(
            document_type=LegalDocumentType.cookie_policy,
            title="New Cookie Policy",
            slug="new-cookie",
            version="2.0",
            is_published=True,
        )

        result = legal.legal_documents.create(db_session, payload)

        db_session.refresh(old_doc)
        assert result.is_current is True
        assert old_doc.is_current is False


# =============================================================================
# Update Tests
# =============================================================================


class TestLegalDocumentUpdate:
    """Tests for updating legal documents."""

    def test_update_basic(self, db_session):
        """Test basic document update."""
        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Original Title",
            slug="update-test",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        payload = LegalDocumentUpdate(title="Updated Title")
        result = legal.legal_documents.update(db_session, str(doc.id), payload)

        assert result is not None
        assert result.title == "Updated Title"

    def test_update_not_found(self, db_session):
        """Test updating non-existent document."""
        payload = LegalDocumentUpdate(title="New Title")
        result = legal.legal_documents.update(db_session, str(uuid.uuid4()), payload)
        assert result is None

    def test_update_publish_sets_timestamp(self, db_session):
        """Test updating to published sets published_at."""
        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Draft",
            slug="draft-to-publish",
            version="1.0",
            is_published=False,
        )
        db_session.add(doc)
        db_session.commit()

        payload = LegalDocumentUpdate(is_published=True)
        result = legal.legal_documents.update(db_session, str(doc.id), payload)

        assert result.is_published is True
        assert result.published_at is not None

    def test_update_set_current(self, db_session):
        """Test setting document as current."""
        old_current = LegalDocument(
            document_type=LegalDocumentType.refund_policy,
            title="Old Refund",
            slug="old-refund",
            version="1.0",
            is_current=True,
        )
        new_doc = LegalDocument(
            document_type=LegalDocumentType.refund_policy,
            title="New Refund",
            slug="new-refund",
            version="2.0",
            is_current=False,
        )
        db_session.add_all([old_current, new_doc])
        db_session.commit()

        payload = LegalDocumentUpdate(is_current=True)
        result = legal.legal_documents.update(db_session, str(new_doc.id), payload)

        db_session.refresh(old_current)
        assert result.is_current is True
        assert old_current.is_current is False


# =============================================================================
# Delete Tests
# =============================================================================


class TestLegalDocumentDelete:
    """Tests for deleting legal documents."""

    def test_delete_success(self, db_session):
        """Test successful document deletion."""
        doc = LegalDocument(
            document_type=LegalDocumentType.other,
            title="To Delete",
            slug="to-delete",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()
        doc_id = str(doc.id)

        result = legal.legal_documents.delete(db_session, doc_id)
        assert result is True

        # Verify deleted
        assert legal.legal_documents.get(db_session, doc_id) is None

    def test_delete_not_found(self, db_session):
        """Test deleting non-existent document."""
        result = legal.legal_documents.delete(db_session, str(uuid.uuid4()))
        assert result is False

    def test_delete_with_file(self, db_session):
        """Test deleting document removes associated file."""
        uploads_dir = os.path.join("uploads", "legal", "tests")
        os.makedirs(uploads_dir, exist_ok=True)
        temp_path = os.path.join(uploads_dir, f"{uuid.uuid4()}.pdf")
        with open(temp_path, "wb") as f:
            f.write(b"test content")

        doc = LegalDocument(
            document_type=LegalDocumentType.other,
            title="With File",
            slug="with-file",
            version="1.0",
            file_path=temp_path,
        )
        db_session.add(doc)
        db_session.commit()

        assert os.path.exists(temp_path)

        legal.legal_documents.delete(db_session, str(doc.id))

        assert not os.path.exists(temp_path)


# =============================================================================
# File Upload Tests
# =============================================================================


class TestLegalDocumentFileUpload:
    """Tests for file upload operations."""

    def test_upload_file(self, db_session, monkeypatch):
        """Test uploading a file."""
        class _Storage:
            def upload(self, key: str, data: bytes, content_type: str | None):
                return None

            def delete(self, key: str):
                return None

        monkeypatch.setattr(legal.file_uploads, "storage", _Storage())

        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="File Upload Test",
            slug="file-upload",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        file_content = b"%PDF-1.4 content here"
        result = legal.legal_documents.upload_file(
            db_session,
            str(doc.id),
            file_content,
            "terms.pdf",
            "application/pdf",
        )

        assert result is not None
        assert result.file_name == "terms.pdf"
        assert result.file_size == len(file_content)
        assert result.mime_type == "application/pdf"
        assert result.file_path is not None
        record = legal.legal_documents.get_active_file_record(db_session, str(doc.id))
        assert record is not None
        assert record.storage_provider == "s3"

    def test_upload_file_not_found(self, db_session):
        """Test uploading to non-existent document."""
        result = legal.legal_documents.upload_file(
            db_session,
            str(uuid.uuid4()),
            b"content",
            "file.pdf",
            "application/pdf",
        )
        assert result is None

    def test_upload_replaces_old_file(self, db_session, monkeypatch):
        """Test uploading replaces existing file."""
        deleted: list[str] = []

        class _Storage:
            def upload(self, key: str, data: bytes, content_type: str | None):
                return None

            def delete(self, key: str):
                deleted.append(key)

        monkeypatch.setattr(legal.file_uploads, "storage", _Storage())

        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Replace Test",
            slug="replace-test",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        # Upload first file
        legal.legal_documents.upload_file(
            db_session, str(doc.id), b"%PDF-1.4 first", "first.pdf", "application/pdf"
        )

        # Upload second file
        updated = legal.legal_documents.upload_file(
            db_session, str(doc.id), b"%PDF-1.4 second", "second.pdf", "application/pdf"
        )

        assert updated is not None
        assert updated.file_name == "second.pdf"
        all_records = (
            db_session.query(StoredFile)
            .filter(StoredFile.entity_type == "legal_document")
            .filter(StoredFile.entity_id == str(doc.id))
            .all()
        )
        assert len(all_records) == 2
        assert len(deleted) == 1
        assert sum(1 for r in all_records if r.is_deleted) == 1


# =============================================================================
# File Delete Tests
# =============================================================================


class TestLegalDocumentFileDelete:
    """Tests for file deletion operations."""

    def test_delete_file(self, db_session, monkeypatch):
        """Test deleting a file."""
        deleted: list[str] = []

        class _Storage:
            def upload(self, key: str, data: bytes, content_type: str | None):
                return None

            def delete(self, key: str):
                deleted.append(key)

        monkeypatch.setattr(legal.file_uploads, "storage", _Storage())

        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="Delete File Test",
            slug="delete-file",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        # Upload a file first
        legal.legal_documents.upload_file(
            db_session, str(doc.id), b"%PDF-1.4 content", "test.pdf", "application/pdf"
        )
        file_path = doc.file_path
        assert file_path is not None

        # Delete the file
        result = legal.legal_documents.delete_file(db_session, str(doc.id))

        assert result is not None
        assert result.file_path is None
        assert result.file_name is None
        assert len(deleted) == 1

    def test_delete_file_not_found(self, db_session):
        """Test deleting file from non-existent document."""
        result = legal.legal_documents.delete_file(db_session, str(uuid.uuid4()))
        assert result is None

    def test_delete_file_no_file(self, db_session):
        """Test deleting when no file exists."""
        doc = LegalDocument(
            document_type=LegalDocumentType.terms_of_service,
            title="No File",
            slug="no-file",
            version="1.0",
        )
        db_session.add(doc)
        db_session.commit()

        result = legal.legal_documents.delete_file(db_session, str(doc.id))
        assert result is not None  # Should still succeed


# =============================================================================
# Module Instance Test
# =============================================================================


def test_singleton_instance():
    """Test legal_documents singleton exists."""
    assert legal.legal_documents is not None
