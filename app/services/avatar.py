import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import settings


def get_allowed_types() -> set[str]:
    return set(settings.avatar_allowed_types.split(","))


def validate_avatar(file: UploadFile) -> None:
    allowed_types = get_allowed_types()
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(allowed_types)}",
        )


async def save_avatar(file: UploadFile, person_id: str) -> str:
    validate_avatar(file)

    upload_dir = Path(settings.avatar_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    content_type = file.content_type
    if content_type is None:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    ext = _get_extension(content_type)
    filename = f"{person_id}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = upload_dir / filename

    content = await file.read()
    if len(content) > settings.avatar_max_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {settings.avatar_max_size_bytes // 1024 // 1024}MB",
        )

    with open(file_path, "wb") as f:
        f.write(content)

    return f"{settings.avatar_url_prefix}/{filename}"


def delete_avatar(avatar_url: str | None) -> None:
    if not avatar_url:
        return
    
    if avatar_url.startswith(settings.avatar_url_prefix):
        # Extract filename safely
        prefix = settings.avatar_url_prefix + "/"
        if not avatar_url.startswith(prefix):
            raise ValueError("Invalid avatar URL format")
        filename = avatar_url[len(prefix):]
        
        # Build and validate path
        base_path = Path(settings.avatar_upload_dir).resolve()
        file_path = (base_path / filename).resolve()
        
        # Ensure the resolved path is within the upload directory
        try:
            file_path.relative_to(base_path)
        except ValueError:
            raise PermissionError("Path escapes upload directory")
        
        if file_path.exists():
            file_path.unlink()


def _get_extension(content_type: str) -> str:
    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    return extensions.get(content_type, ".jpg")
