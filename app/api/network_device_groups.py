"""API routes for network device groups."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.network import (
    DeviceGroupActionRequest,
    DeviceGroupCreate,
    DeviceGroupMemberCreate,
    DeviceGroupUpdate,
)
from app.services.auth_dependencies import require_permission
from app.services.network import device_groups as device_group_service

router = APIRouter(prefix="/network/device-groups", tags=["network-device-groups"])


@router.get("", dependencies=[Depends(require_permission("network:device:read"))])
def list_device_groups(db: Session = Depends(get_db)) -> dict:
    rows = device_group_service.list_device_groups(db)
    return {
        "items": [
            {
                "id": str(row["group"].id),
                "name": row["group"].name,
                "kind": row["group"].kind,
                "description": row["group"].description,
                "is_active": row["group"].is_active,
                "ont_count": row["ont_count"],
                "cpe_count": row["cpe_count"],
                "member_count": row["member_count"],
            }
            for row in rows
        ]
    }


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def create_device_group(
    payload: DeviceGroupCreate,
    db: Session = Depends(get_db),
) -> dict:
    try:
        group = device_group_service.create_device_group_committed(
            db,
            name=payload.name,
            kind=payload.kind,
            description=payload.description,
        )
        return {"id": str(group.id), "name": group.name}
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{group_id}", dependencies=[Depends(require_permission("network:device:read"))]
)
def get_device_group(group_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        context = device_group_service.device_group_detail_context(db, group_id)
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": str(context["group"].id),
        "name": context["group"].name,
        "description": context["group"].description,
        "ont_count": context["ont_count"],
        "cpe_count": context["cpe_count"],
        "members": [
            {
                "id": str(row["member"].id),
                "device_type": row["member"].device_type,
                "device_id": str(row["member"].device_id),
                "label": row["label"],
            }
            for row in context["member_rows"]
        ],
    }


@router.patch(
    "/{group_id}",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def update_device_group(
    group_id: str,
    payload: DeviceGroupUpdate,
    db: Session = Depends(get_db),
) -> dict:
    try:
        group = device_group_service.update_device_group_committed(
            db,
            group_id=group_id,
            name=payload.name,
            description=payload.description,
        )
        return {"id": str(group.id), "name": group.name}
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/{group_id}",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def archive_device_group(group_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        group = device_group_service.archive_device_group_committed(
            db, group_id=group_id
        )
        return {"id": str(group.id), "archived": True}
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{group_id}/members",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def add_device_group_member(
    group_id: str,
    payload: DeviceGroupMemberCreate,
    db: Session = Depends(get_db),
) -> dict:
    try:
        member = device_group_service.add_device_group_member_committed(
            db,
            group_id=group_id,
            device_type=payload.device_type,
            device_id=payload.device_id,
        )
        return {"id": str(member.id)}
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/{group_id}/members/{member_id}",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def remove_device_group_member(
    group_id: str,
    member_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        device_group_service.remove_device_group_member_committed(
            db,
            group_id=group_id,
            member_id=member_id,
        )
        return {"removed": True}
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{group_id}/actions",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def queue_device_group_action(
    group_id: str,
    payload: DeviceGroupActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = device_group_service.enqueue_ont_group_action_committed(
            db,
            group_id=group_id,
            action=payload.action,
            params=payload.params,
        )
        return result
    except device_group_service.DeviceGroupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
