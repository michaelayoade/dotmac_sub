"""Safe CRUD and query policy for OLT and ONT firmware artifacts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OltFirmwareImage, OntFirmwareImage
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)

FIRMWARE_KINDS = {"olt", "ont"}
_ACTIVE_OPERATION_STATUSES = {
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
}
_ARTIFACT_FIELDS = {
    "vendor",
    "model",
    "version",
    "file_url",
    "filename",
    "checksum",
    "file_size_bytes",
    "upgrade_method",
}
_SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})\Z")


class FirmwareCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwareUsage:
    reference_count: int = 0
    active_reference_count: int = 0

    @property
    def used(self) -> bool:
        return self.reference_count > 0

    @property
    def active(self) -> bool:
        return self.active_reference_count > 0


@dataclass(frozen=True)
class FirmwareCatalogRow:
    kind: str
    image: OltFirmwareImage | OntFirmwareImage
    usage: FirmwareUsage


def model_for_kind(kind: str) -> type[OltFirmwareImage] | type[OntFirmwareImage]:
    normalized = str(kind or "").strip().lower()
    if normalized == "olt":
        return OltFirmwareImage
    if normalized == "ont":
        return OntFirmwareImage
    raise FirmwareCatalogError("Firmware type must be OLT or ONT.")


def normalize_form_values(values: dict[str, object]) -> dict[str, object]:
    kind = str(values.get("kind") or "").strip().lower()
    vendor = str(values.get("vendor") or "").strip()
    model = str(values.get("model") or "").strip() or None
    version = str(values.get("version") or "").strip()
    file_url = str(values.get("file_url") or "").strip()
    filename = str(values.get("filename") or "").strip() or None
    checksum = str(values.get("checksum") or "").strip()
    notes = str(values.get("notes") or "").strip() or None
    release_notes = str(values.get("release_notes") or "").strip() or None
    raw_size = str(values.get("file_size_bytes") or "").strip()
    try:
        file_size_bytes = int(raw_size) if raw_size else None
    except ValueError as exc:
        raise FirmwareCatalogError(
            "File size must be a whole number of bytes."
        ) from exc

    if kind not in FIRMWARE_KINDS:
        raise FirmwareCatalogError("Firmware type must be OLT or ONT.")
    if not vendor:
        raise FirmwareCatalogError("Vendor is required.")
    if not version:
        raise FirmwareCatalogError("Version is required.")
    if not file_url:
        raise FirmwareCatalogError("Firmware URL is required.")
    if any(char.isspace() for char in file_url) or any(
        char in file_url for char in ";|<>"
    ):
        raise FirmwareCatalogError("Firmware URL contains unsafe characters.")

    parsed = urlparse(file_url)
    allowed_schemes = {"sftp", "ftp", "tftp"} if kind == "olt" else {"https", "http"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
        schemes = ", ".join(sorted(allowed_schemes))
        raise FirmwareCatalogError(f"{kind.upper()} firmware URL must use {schemes}.")
    if parsed.username or parsed.password:
        raise FirmwareCatalogError(
            "Firmware URL must not contain credentials; use managed device credentials."
        )

    checksum_match = _SHA256_RE.fullmatch(checksum)
    if checksum_match is None:
        raise FirmwareCatalogError("A full SHA-256 checksum is required.")
    checksum = f"sha256:{checksum_match.group(1).lower()}"
    if file_size_bytes is not None and file_size_bytes <= 0:
        raise FirmwareCatalogError("File size must be greater than zero.")

    return {
        "kind": kind,
        "vendor": vendor,
        "model": model,
        "version": version,
        "file_url": file_url,
        "filename": filename,
        "checksum": checksum,
        "file_size_bytes": file_size_bytes,
        "upgrade_method": parsed.scheme.lower() if kind == "olt" else None,
        "release_notes": release_notes,
        "notes": notes,
        "is_active": bool(values.get("is_active")),
    }


def _usage_by_image_id(db: Session) -> dict[str, FirmwareUsage]:
    operation_types = {
        NetworkOperationType.olt_firmware_upgrade,
        NetworkOperationType.ont_firmware_upgrade,
    }
    rows = db.execute(
        select(NetworkOperation.status, NetworkOperation.input_payload).where(
            NetworkOperation.operation_type.in_(operation_types)
        )
    ).all()
    totals: dict[str, int] = {}
    active: dict[str, int] = {}
    for status, input_payload in rows:
        image_id = str((input_payload or {}).get("firmware_image_id") or "")
        if not image_id:
            continue
        totals[image_id] = totals.get(image_id, 0) + 1
        if status in _ACTIVE_OPERATION_STATUSES:
            active[image_id] = active.get(image_id, 0) + 1
    return {
        image_id: FirmwareUsage(count, active.get(image_id, 0))
        for image_id, count in totals.items()
    }


def image_usage(db: Session, image_id: object) -> FirmwareUsage:
    return _usage_by_image_id(db).get(str(image_id), FirmwareUsage())


def get_image(db: Session, kind: str, image_id: str):
    model = model_for_kind(kind)
    try:
        image_uuid = uuid.UUID(str(image_id))
    except (TypeError, ValueError) as exc:
        raise FirmwareCatalogError("Firmware image not found.") from exc
    image = db.get(model, image_uuid)
    if image is None:
        raise FirmwareCatalogError("Firmware image not found.")
    return image


def list_images(
    db: Session,
    *,
    kind: str = "all",
    search: str | None = None,
    vendor: str | None = None,
    status: str = "all",
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    selected_kinds = [kind] if kind in FIRMWARE_KINDS else ["olt", "ont"]
    usage = _usage_by_image_id(db)
    rows: list[FirmwareCatalogRow] = []
    for selected_kind in selected_kinds:
        model = model_for_kind(selected_kind)
        query = select(model)
        if search:
            term = f"%{search.strip()}%"
            query = query.where(
                or_(
                    model.vendor.ilike(term),
                    model.model.ilike(term),
                    model.version.ilike(term),
                    model.filename.ilike(term),
                )
            )
        if vendor:
            query = query.where(func.lower(model.vendor) == vendor.strip().lower())
        if status == "active":
            query = query.where(model.is_active.is_(True))
        elif status == "inactive":
            query = query.where(model.is_active.is_(False))
        for raw_image in db.scalars(query).all():
            image = cast(OltFirmwareImage | OntFirmwareImage, raw_image)
            rows.append(
                FirmwareCatalogRow(
                    kind=selected_kind,
                    image=image,
                    usage=usage.get(str(image.id), FirmwareUsage()),
                )
            )
    rows.sort(
        key=lambda row: (
            row.image.vendor.lower(),
            str(row.image.model or "").lower(),
            row.image.version.lower(),
        ),
        reverse=True,
    )
    total = len(rows)
    page = max(1, page)
    per_page = min(100, max(10, per_page))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    vendors = sorted(
        {
            str(value)
            for model in (OltFirmwareImage, OntFirmwareImage)
            for value in db.scalars(select(model.vendor).distinct()).all()
            if value
        },
        key=str.lower,
    )
    return {
        "rows": rows[start : start + per_page],
        "vendors": vendors,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < pages,
        },
    }


def create_image(db: Session, values: dict[str, object]):
    normalized = normalize_form_values(values)
    kind = str(normalized.pop("kind"))
    release_notes = normalized.pop("release_notes", None)
    if kind == "olt":
        normalized["release_notes"] = release_notes
    else:
        normalized.pop("upgrade_method", None)
        if release_notes and not normalized.get("notes"):
            normalized["notes"] = release_notes
    image = model_for_kind(kind)(**normalized)
    db.add(image)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise FirmwareCatalogError(
            "This vendor, model, and version already exists."
        ) from exc
    db.refresh(image)
    return image


def update_image(db: Session, kind: str, image_id: str, values: dict[str, object]):
    image = get_image(db, kind, image_id)
    normalized = normalize_form_values({**values, "kind": kind})
    normalized.pop("kind", None)
    release_notes = normalized.pop("release_notes", None)
    usage = image_usage(db, image.id)
    if usage.active:
        raise FirmwareCatalogError(
            "This image has an active upgrade and cannot be changed or deactivated."
        )
    if usage.used:
        changed = [
            field
            for field in _ARTIFACT_FIELDS
            if field != "upgrade_method"
            and getattr(image, field, None) != normalized.get(field)
        ]
        if changed:
            raise FirmwareCatalogError(
                "Artifact identity is immutable after rollout; create a new image version."
            )
    for field, value in normalized.items():
        if field == "upgrade_method" and kind != "olt":
            continue
        setattr(image, field, value)
    if kind == "olt":
        image.release_notes = release_notes
    elif release_notes:
        image.notes = str(release_notes)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise FirmwareCatalogError(
            "This vendor, model, and version already exists."
        ) from exc
    db.refresh(image)
    return image


def deactivate_image(db: Session, kind: str, image_id: str) -> None:
    image = get_image(db, kind, image_id)
    usage = image_usage(db, image.id)
    if usage.active:
        raise FirmwareCatalogError(
            "This image has an active upgrade and cannot be deactivated."
        )
    image.is_active = False
    db.commit()
