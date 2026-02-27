from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.stored_file import StoredFile
from app.services.file_storage import file_uploads
from app.web.admin import system as admin_system


def _build_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


@pytest.mark.parametrize(
    "blocked_path",
    (
        lambda upload_root, outside: upload_root / ".." / outside.name,
        lambda _upload_root, outside: outside,
    ),
    ids=("dot-dot-traversal", "absolute-outside"),
)
def test_file_storage_denies_paths_outside_upload_root(
    tmp_path,
    monkeypatch,
    blocked_path,
):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"blocked")
    candidate_path = blocked_path(upload_root, outside_file)

    monkeypatch.setattr(
        "app.services.file_storage.settings",
        SimpleNamespace(base_upload_dir=str(upload_root)),
    )

    record = StoredFile(
        organization_id=None,
        entity_type="test",
        entity_id="1",
        original_filename="outside.txt",
        storage_key_or_relative_path="legacy/path",
        legacy_local_path=str(candidate_path),
        file_size=7,
        content_type="text/plain",
        storage_provider="local",
        uploaded_by=None,
    )

    with pytest.raises(
        PermissionError, match="Access denied: path outside upload directory"
    ):
        file_uploads.stream_file(record)


@pytest.mark.parametrize(
    "blocked_file_path",
    (
        lambda export_root, outside: export_root / ".." / outside.name,
        lambda _export_root, outside: outside,
    ),
    ids=("dot-dot-traversal", "absolute-outside"),
)
def test_admin_export_download_rejects_paths_outside_export_directory(
    db_session,
    tmp_path,
    monkeypatch,
    blocked_file_path,
):
    export_root = tmp_path / "exports"
    export_root.mkdir()
    outside_file = tmp_path / "outside.csv"
    outside_file.write_text("id,name\n1,test\n", encoding="utf-8")

    monkeypatch.setattr(
        admin_system,
        "settings",
        SimpleNamespace(export_jobs_base_dir=str(export_root)),
    )
    monkeypatch.setattr(
        admin_system.web_system_export_tool_service,
        "get_export_job",
        lambda _db, _job_id: {
            "status": "completed",
            "file_path": str(blocked_file_path(export_root, outside_file)),
            "filename": "report.csv",
            "module": "subscribers",
        },
    )
    monkeypatch.setattr(
        admin_system.web_system_export_tool_service,
        "log_export_audit_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"person_id": None},
    )

    with pytest.raises(HTTPException) as exc:
        admin_system.system_export_job_download(
            request=_build_request("/admin/system/export/jobs/job-1/download"),
            job_id="job-1",
            db=db_session,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied"
