from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Request

from app.web.admin import nas


def test_generate_radius_shared_secret_is_strong_alphanumeric_mixed() -> None:
    secret = nas._generate_radius_shared_secret()

    # Default is now a strong 32 chars (was a weak 6); floor of 16, configurable.
    assert len(secret) == 32
    assert secret.isalnum()
    assert any(char.isalpha() for char in secret)
    assert any(char.isdigit() for char in secret)


def test_device_form_new_includes_generated_radius_secret() -> None:
    request = Request(
        {"type": "http", "method": "GET", "path": "/admin/network/nas/devices/new"}
    )
    template_response = MagicMock(name="template_response")

    with (
        patch("app.web.admin.nas._base_context", return_value={"request": request}),
        patch("app.web.admin.nas._get_form_options", return_value={}),
        patch(
            "app.web.admin.nas.templates.TemplateResponse",
            return_value=template_response,
        ) as render,
    ):
        response = nas.device_form_new(request=request, db=SimpleNamespace())

    assert response is template_response
    assert render.call_args.args[0] == "admin/network/nas/device_form.html"
    context = render.call_args.args[1]
    secret = context["generated_radius_secret"]
    assert len(secret) == 32
    assert secret.isalnum()
    assert any(char.isalpha() for char in secret)
    assert any(char.isdigit() for char in secret)


def test_build_backup_redirect_url_uses_referer_backups_page() -> None:
    device_id = "2b7a42c6-41f9-46c0-af55-ef805cd076b2"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/network/nas/devices/{device_id}/backups/trigger",
            "headers": [
                (
                    b"referer",
                    f"http://testserver/admin/network/nas/devices/{device_id}/backups?page=2".encode(),
                )
            ],
        }
    )

    url = nas._build_backup_redirect_url(
        request,
        device_id=device_id,
        key="message",
        value="Backup triggered successfully",
    )

    assert url == (
        f"/admin/network/nas/devices/{device_id}/backups"
        "?page=2&message=Backup+triggered+successfully"
    )


def test_build_backup_redirect_url_falls_back_to_backups_page() -> None:
    device_id = "2b7a42c6-41f9-46c0-af55-ef805cd076b2"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/network/nas/devices/{device_id}/backups/trigger",
            "headers": [(b"referer", b"http://testserver/admin/network/nas")],
        }
    )

    url = nas._build_backup_redirect_url(
        request,
        device_id=device_id,
        key="error",
        value="Backup failed",
    )

    assert url == f"/admin/network/nas/devices/{device_id}/backups?error=Backup+failed"


def test_backup_download_returns_attachment_response() -> None:
    backup = SimpleNamespace(
        id="backup-123",
        config_content="/export compact",
        config_format="rsc",
    )
    device = SimpleNamespace(id="device-456")

    with patch(
        "app.web.admin.nas.nas_service.build_nas_backup_detail_data",
        return_value={"backup": backup, "device": device, "activities": []},
    ):
        response = nas.backup_download(
            backup_id="backup-123",
            db=SimpleNamespace(),
        )

    assert response.status_code == 200
    assert response.body == b"/export compact"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="nas_backup_device-456_backup-123.rsc"'
    )
