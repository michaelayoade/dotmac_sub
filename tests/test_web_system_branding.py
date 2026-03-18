from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from app.web.admin import system as system_web


def test_settings_branding_update_ignores_non_subscriber_uploaded_by(db_session, monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"subscriber_id": "87bdb5e4-d626-4541-9aff-25b04b0423a4"},
    )

    def _fake_upload_branding_asset(**kwargs):
        captured["uploaded_by"] = kwargs["uploaded_by"]
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(
        system_web.branding_storage_service,
        "upload_branding_asset",
        _fake_upload_branding_asset,
    )

    request = Request({"type": "http", "method": "POST", "path": "/admin/system/settings/branding", "headers": []})
    upload = UploadFile(
        filename="logo.png",
        file=BytesIO(b"fake png"),
        headers=Headers({"content-type": "image/png"}),
    )

    response = system_web.settings_branding_update(
        request=request,
        main_logo_url=None,
        dark_logo_url=None,
        favicon_url=None,
        remove_main_logo=None,
        remove_dark_logo=None,
        remove_favicon=None,
        main_logo_file=upload,
        dark_logo_file=None,
        favicon_file=None,
        db=db_session,
    )

    assert captured["uploaded_by"] is None
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system/branding"
