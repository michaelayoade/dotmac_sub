"""Brand-logo resolver and statement renderer refuse unsafe logo sources."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import brand_profiles
from app.services import customer_account_statement as statements
from app.services.brand_profiles import MAX_LOGO_BYTES, resolve_brand_logo_source

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    base = tmp_path / "static"
    (base / "branding").mkdir(parents=True)
    monkeypatch.setattr(brand_profiles, "_STATIC_BASE", base)
    return base


def _resolve(value: str | None) -> str | None:
    return resolve_brand_logo_source(object(), configured_logo=value)  # type: ignore[arg-type]


def test_static_logo_is_embedded(static_dir) -> None:
    (static_dir / "branding" / "logo.png").write_bytes(PNG)
    result = _resolve("/static/branding/logo.png")
    assert result == "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_static_path_escape_is_refused(static_dir) -> None:
    (static_dir.parent / "secret.png").write_bytes(PNG)
    assert _resolve("/static/../secret.png") is None
    assert _resolve("/static/branding/../../secret.png") is None


def test_static_non_image_and_oversize_are_refused(static_dir) -> None:
    (static_dir / "branding" / "notes.txt").write_bytes(b"hello")
    (static_dir / "branding" / "logo.svg").write_bytes(b"<svg/>")
    (static_dir / "branding" / "big.png").write_bytes(b"0" * (MAX_LOGO_BYTES + 1))
    assert _resolve("/static/branding/notes.txt") is None
    assert _resolve("/static/branding/logo.svg") is None
    assert _resolve("/static/branding/big.png") is None
    assert _resolve("/static/branding/missing.png") is None


@pytest.mark.parametrize(
    "value", ["https://example.com/logo.png", "http://192.0.2.10/logo.png"]
)
def test_remote_logo_urls_are_refused(value: str) -> None:
    assert _resolve(value) is None


def _stored(monkeypatch, *, content: bytes, content_type: str, size=None):
    from app.services import branding_storage
    from app.services.file_storage import file_uploads

    record = SimpleNamespace(
        is_deleted=False,
        content_type=content_type,
        file_size=len(content) if size is None else size,
    )
    monkeypatch.setattr(branding_storage, "file_id_from_branding_url", lambda _v: "id")
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda _r: SimpleNamespace(chunks=iter([content[:10], content[10:]])),
    )
    return SimpleNamespace(get=lambda _model, _id: record)


def test_stored_asset_is_accepted(monkeypatch) -> None:
    db = _stored(monkeypatch, content=PNG, content_type="image/png")
    result = resolve_brand_logo_source(db, configured_logo="/branding/assets/abc")
    assert result == "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_stored_asset_oversize_or_wrong_type_is_refused(monkeypatch) -> None:
    big = b"0" * (MAX_LOGO_BYTES + 1)
    db = _stored(monkeypatch, content=big, content_type="image/png")
    assert resolve_brand_logo_source(db, configured_logo="/branding/assets/a") is None
    # Declared size understates the stream; the streamed read is capped too.
    db = _stored(monkeypatch, content=big, content_type="image/png", size=10)
    assert resolve_brand_logo_source(db, configured_logo="/branding/assets/a") is None
    db = _stored(monkeypatch, content=PNG, content_type="text/html")
    assert resolve_brand_logo_source(db, configured_logo="/branding/assets/a") is None


def _document(legal_name: str) -> statements.CustomerStatementDocument:
    statement = statements.CustomerStatement(
        account_id=uuid4(),
        account_label="Account",
        period=statements.CustomerStatementRange(
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
        ),
        balances=(),
        rows=(),
        generated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    brand = statements.CustomerStatementBrand(
        legal_name=legal_name,
        product_name="Dotmac",
        tagline="",
        primary_color="#206a07",
        primary_dark_color="#103504",
        primary_tint_color="#f1f7ef",
        secondary_color="#06b6d4",
        logo_source=None,
        support_email="",
        support_phone="",
        address_lines=(),
        source_scope="platform",
        source_scope_id=None,
    )
    return statements.CustomerStatementDocument(statement=statement, brand=brand)


def test_footer_name_cannot_close_the_style_element() -> None:
    html = statements.render_statement_html(_document('Evil </style><b>x</b> "q"'))
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "<" not in style
    assert "\\3c /style>" in style


def test_statement_fetcher_refuses_everything_but_data_uris() -> None:
    with pytest.raises(ValueError):
        statements._data_uri_only_fetcher("https://example.com/logo.png")
    with pytest.raises(ValueError):
        statements._data_uri_only_fetcher("file:///etc/passwd")
    try:
        result = statements._data_uri_only_fetcher(
            "data:image/png;base64," + base64.b64encode(PNG).decode()
        )
    except OSError:  # WeasyPrint native libraries (pango) not installed
        pytest.skip("WeasyPrint native libraries unavailable")
    assert result["string"] == PNG or result.get("file_obj") is not None
