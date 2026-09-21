from __future__ import annotations

import pytest

from app.services.domain_errors import DomainError
from app.services.field.attachments import resolve_expense_receipt_file_identity


def test_receipt_identity_uses_jpeg_bytes_instead_of_png_label():
    identity = resolve_expense_receipt_file_identity(
        file_name="scaled_42.png",
        content=b"\xff\xd8\xff\xe0jpeg",
    )

    assert identity.file_name == "scaled_42.jpg"
    assert identity.mime_type == "image/jpeg"


def test_receipt_identity_rejects_unknown_content():
    with pytest.raises(DomainError):
        resolve_expense_receipt_file_identity(
            file_name="receipt.png",
            content=b"not-an-image",
        )
