from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, not_, or_
from sqlalchemy.orm import Session, selectinload

from app.models.field_asset import FieldAsset, FieldAssetCustody
from app.models.field_material import FieldInventoryItem
from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import Router

ASSET_SOURCES = {
    "field_inventory",
    "field_asset",
    "ont",
    "cpe",
    "olt",
    "network_device",
    "router",
}


@dataclass(frozen=True)
class AssetCatalogFilters:
    source: str | None = None
    q: str | None = None
    status: str | None = None
    subscriber_id: str | None = None
    assigned_to_technician_id: str | None = None
    assigned_to_system_user_id: str | None = None
    limit: int = 50
    offset: int = 0


class AssetInventory:
    """Read-only catalog over sub-native material and operational asset sources."""

    @staticmethod
    def list_catalog(db: Session, filters: AssetCatalogFilters) -> dict:
        safe_limit = max(1, min(int(filters.limit or 50), 200))
        safe_offset = max(0, int(filters.offset or 0))
        query_limit = safe_limit + safe_offset

        selected_sources = _selected_sources(filters.source)
        summary = _summary(db, selected_sources, filters)

        rows: list[dict] = []
        if "field_inventory" in selected_sources:
            rows.extend(_field_inventory_rows(db, filters, query_limit))
        if "field_asset" in selected_sources:
            rows.extend(_field_asset_rows(db, filters, query_limit))
        if "ont" in selected_sources:
            rows.extend(_ont_rows(db, filters, query_limit))
        if "cpe" in selected_sources:
            rows.extend(_cpe_rows(db, filters, query_limit))
        if "olt" in selected_sources:
            rows.extend(_olt_rows(db, filters, query_limit))
        if "network_device" in selected_sources:
            rows.extend(_network_device_rows(db, filters, query_limit))
        if "router" in selected_sources:
            rows.extend(_router_rows(db, filters, query_limit))

        rows.sort(
            key=lambda item: (item["label"].lower(), item["source"], str(item["id"]))
        )
        return {
            "items": rows[safe_offset : safe_offset + safe_limit],
            "count": summary["total"],
            "limit": safe_limit,
            "offset": safe_offset,
            "summary": summary,
        }


def _selected_sources(source: str | None) -> set[str]:
    if not source:
        return set(ASSET_SOURCES)
    normalized = source.strip().lower()
    if normalized not in ASSET_SOURCES:
        return set()
    return {normalized}


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _metadata(value: object | None) -> dict | None:
    if isinstance(value, dict):
        return value
    return None


def _matches_search(columns: list[Any], q: str | None) -> Any | None:
    term = (q or "").strip()
    if not term:
        return None
    pattern = f"%{term}%"
    return or_(*[column.ilike(pattern) for column in columns])


def _summary(db: Session, sources: set[str], filters: AssetCatalogFilters) -> dict:
    counts = {
        "field_inventory": _field_inventory_query(db, filters).count()
        if "field_inventory" in sources
        else 0,
        "field_asset": _field_asset_query(db, filters).count()
        if "field_asset" in sources
        else 0,
        "ont": _ont_query(db, filters).count() if "ont" in sources else 0,
        "cpe": _cpe_query(db, filters).count() if "cpe" in sources else 0,
        "olt": _olt_query(db, filters).count() if "olt" in sources else 0,
        "network_device": _network_device_query(db, filters).count()
        if "network_device" in sources
        else 0,
        "router": _router_query(db, filters).count() if "router" in sources else 0,
    }
    counts["total"] = sum(counts.values())
    return counts


def _field_inventory_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(FieldInventoryItem).filter(FieldInventoryItem.is_active.is_(True))
    search = _matches_search(
        [
            FieldInventoryItem.name,
            FieldInventoryItem.sku,
            FieldInventoryItem.crm_item_id,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        status = filters.status.strip().lower()
        if status not in {"active", "inactive"}:
            return query.filter(false())
        query = query.filter(FieldInventoryItem.is_active.is_(status == "active"))
    if filters.subscriber_id:
        return query.filter(false())
    return _apply_custody_filters(
        query, FieldInventoryItem.id, "field_inventory", filters
    )


def _field_inventory_rows(
    db: Session, filters: AssetCatalogFilters, limit: int
) -> list[dict]:
    rows = (
        _field_inventory_query(db, filters)
        .order_by(FieldInventoryItem.name.asc(), FieldInventoryItem.created_at.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "field_inventory", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "field_inventory",
            "asset_type": "material",
            "label": row.name,
            "identifier": row.sku or row.crm_item_id,
            "status": "active" if row.is_active else "inactive",
            "vendor": None,
            "model": None,
            "serial_number": None,
            "management_ip": None,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": None,
            "metadata": _metadata(row.metadata_),
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _field_asset_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(FieldAsset).filter(FieldAsset.is_active.is_(True))
    search = _matches_search(
        [
            FieldAsset.name,
            FieldAsset.asset_tag,
            FieldAsset.serial_number,
            FieldAsset.registration_number,
            FieldAsset.vendor,
            FieldAsset.model,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        query = query.filter(FieldAsset.status == filters.status.strip().lower())
    if filters.subscriber_id:
        return query.filter(false())
    return _apply_custody_filters(query, FieldAsset.id, "field_asset", filters)


def _field_asset_rows(
    db: Session, filters: AssetCatalogFilters, limit: int
) -> list[dict]:
    rows = (
        _field_asset_query(db, filters)
        .order_by(FieldAsset.name.asc(), FieldAsset.asset_tag.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "field_asset", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "field_asset",
            "asset_type": row.asset_type,
            "label": row.name,
            "identifier": row.asset_tag,
            "status": row.status,
            "vendor": row.vendor,
            "model": row.model,
            "serial_number": row.serial_number,
            "management_ip": None,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": row.registration_number,
            "metadata": {
                **(_metadata(row.metadata_) or {}),
                "asset_tag": row.asset_tag,
                "registration_number": row.registration_number,
                "condition": row.condition,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _ont_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(OntUnit).filter(OntUnit.is_active.is_(True))
    search = _matches_search(
        [
            OntUnit.name,
            OntUnit.serial_number,
            OntUnit.vendor_serial_number,
            OntUnit.model,
            OntUnit.vendor,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        from app.services.network.ont_status import effective_ont_online_clause

        normalized_status = filters.status.strip().lower()
        if normalized_status in {"online", "offline"}:
            online_clause = effective_ont_online_clause()
            query = query.filter(
                online_clause if normalized_status == "online" else not_(online_clause)
            )
    if filters.subscriber_id:
        query = query.filter(false())
    return _apply_custody_filters(query, OntUnit.id, "ont", filters)


def _ont_rows(db: Session, filters: AssetCatalogFilters, limit: int) -> list[dict]:
    rows = (
        _ont_query(db, filters)
        .order_by(OntUnit.name.asc().nullslast(), OntUnit.serial_number.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "ont", [row.id for row in rows])
    from app.services.network.ont_status import resolve_effective_ont_status

    return [
        {
            "id": row.id,
            "source": "ont",
            "asset_type": "ont",
            "label": row.name or row.serial_number,
            "identifier": row.serial_number,
            "status": resolve_effective_ont_status(row).status.value,
            "vendor": row.vendor,
            "model": row.model,
            "serial_number": row.serial_number,
            "management_ip": None,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": row.address_or_comment,
            "metadata": {
                "vendor_serial_number": row.vendor_serial_number,
                "olt_device_id": str(row.olt_device_id) if row.olt_device_id else None,
                "pon_port_id": str(row.pon_port_id) if row.pon_port_id else None,
                "external_id": row.external_id,
                "uisp_device_id": row.uisp_device_id,
                "raw_olt_status": _enum_value(row.olt_status),
                "status_retry_pending": resolve_effective_ont_status(row).retry_pending,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _cpe_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(CPEDevice)
    search = _matches_search(
        [
            CPEDevice.serial_number,
            CPEDevice.model,
            CPEDevice.vendor,
            CPEDevice.mac_address,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        query = query.filter(CPEDevice.status == filters.status.strip().lower())
    if filters.subscriber_id:
        query = query.filter(CPEDevice.subscriber_id == filters.subscriber_id)
    return _apply_custody_filters(query, CPEDevice.id, "cpe", filters)


def _cpe_rows(db: Session, filters: AssetCatalogFilters, limit: int) -> list[dict]:
    rows = (
        _cpe_query(db, filters)
        .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "cpe", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "cpe",
            "asset_type": _enum_value(row.device_type) or "cpe",
            "label": row.serial_number or row.mac_address or str(row.id),
            "identifier": row.mac_address or row.serial_number,
            "status": _enum_value(row.status),
            "vendor": row.vendor,
            "model": row.model,
            "serial_number": row.serial_number,
            "management_ip": None,
            "subscriber_id": row.subscriber_id,
            **_custody_fields(custody.get(row.id)),
            "location": None,
            "metadata": {
                "mac_address": row.mac_address,
                "parent_network_device_id": str(row.parent_network_device_id)
                if row.parent_network_device_id
                else None,
                "uisp_device_id": row.uisp_device_id,
                "last_uisp_status": row.last_uisp_status,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _olt_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(OLTDevice).filter(OLTDevice.is_active.is_(True))
    search = _matches_search(
        [
            OLTDevice.name,
            OLTDevice.hostname,
            OLTDevice.mgmt_ip,
            OLTDevice.vendor,
            OLTDevice.model,
            OLTDevice.serial_number,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        query = query.filter(OLTDevice.status == filters.status.strip().lower())
    if filters.subscriber_id:
        return query.filter(false())
    return _apply_custody_filters(query, OLTDevice.id, "olt", filters)


def _olt_rows(db: Session, filters: AssetCatalogFilters, limit: int) -> list[dict]:
    rows = (
        _olt_query(db, filters)
        .order_by(OLTDevice.name.asc(), OLTDevice.created_at.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "olt", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "olt",
            "asset_type": "olt",
            "label": row.name,
            "identifier": row.hostname or row.mgmt_ip,
            "status": _enum_value(row.status),
            "vendor": row.vendor,
            "model": row.model,
            "serial_number": row.serial_number,
            "management_ip": row.mgmt_ip,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": None,
            "metadata": {
                "hostname": row.hostname,
                "last_ping_ok": row.last_ping_ok,
                "last_poll_status": _enum_value(row.last_poll_status),
                "uisp_device_id": row.uisp_device_id,
                "zabbix_host_id": row.zabbix_host_id,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _network_device_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True))
    search = _matches_search(
        [
            NetworkDevice.name,
            NetworkDevice.hostname,
            NetworkDevice.mgmt_ip,
            NetworkDevice.vendor,
            NetworkDevice.model,
            NetworkDevice.serial_number,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        query = query.filter(NetworkDevice.status == filters.status.strip().lower())
    if filters.subscriber_id:
        return query.filter(false())
    return _apply_custody_filters(query, NetworkDevice.id, "network_device", filters)


def _network_device_rows(
    db: Session, filters: AssetCatalogFilters, limit: int
) -> list[dict]:
    rows = (
        _network_device_query(db, filters)
        .order_by(NetworkDevice.name.asc(), NetworkDevice.created_at.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "network_device", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "network_device",
            "asset_type": _enum_value(row.device_type) or "network_device",
            "label": row.name,
            "identifier": row.hostname or row.mgmt_ip,
            "status": row.live_status or _enum_value(row.status),
            "vendor": row.vendor,
            "model": row.model,
            "serial_number": row.serial_number,
            "management_ip": row.mgmt_ip,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": None,
            "metadata": {
                "hostname": row.hostname,
                "role": _enum_value(row.role),
                "zabbix_hostid": row.zabbix_hostid,
                "uisp_device_id": row.uisp_device_id,
                "matched_device_type": row.matched_device_type,
                "matched_device_id": str(row.matched_device_id)
                if row.matched_device_id
                else None,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _router_query(db: Session, filters: AssetCatalogFilters):
    query = db.query(Router).filter(Router.is_active.is_(True))
    search = _matches_search(
        [
            Router.name,
            Router.hostname,
            Router.management_ip,
            Router.board_name,
            Router.serial_number,
        ],
        filters.q,
    )
    if search is not None:
        query = query.filter(search)
    if filters.status:
        query = query.filter(Router.status == filters.status.strip().lower())
    if filters.subscriber_id:
        return query.filter(false())
    return _apply_custody_filters(query, Router.id, "router", filters)


def _router_rows(db: Session, filters: AssetCatalogFilters, limit: int) -> list[dict]:
    rows = (
        _router_query(db, filters)
        .order_by(Router.name.asc(), Router.created_at.asc())
        .limit(limit)
        .all()
    )
    custody = _custody_map(db, "router", [row.id for row in rows])
    return [
        {
            "id": row.id,
            "source": "router",
            "asset_type": "router",
            "label": row.name,
            "identifier": row.hostname or row.management_ip,
            "status": _enum_value(row.status),
            "vendor": "MikroTik" if row.routeros_version or row.board_name else None,
            "model": row.board_name,
            "serial_number": row.serial_number,
            "management_ip": row.management_ip,
            "subscriber_id": None,
            **_custody_fields(custody.get(row.id)),
            "location": row.location,
            "metadata": {
                "hostname": row.hostname,
                "routeros_version": row.routeros_version,
                "network_device_id": str(row.network_device_id)
                if row.network_device_id
                else None,
                "nas_device_id": str(row.nas_device_id) if row.nas_device_id else None,
            },
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _apply_custody_filters(query, id_column, source: str, filters: AssetCatalogFilters):
    if not filters.assigned_to_technician_id and not filters.assigned_to_system_user_id:
        return query
    query = query.join(
        FieldAssetCustody,
        and_(
            FieldAssetCustody.asset_source == source,
            FieldAssetCustody.asset_id == id_column,
            FieldAssetCustody.status == "issued",
        ),
    )
    if filters.assigned_to_technician_id:
        query = query.filter(
            FieldAssetCustody.technician_id == filters.assigned_to_technician_id
        )
    if filters.assigned_to_system_user_id:
        query = query.filter(
            FieldAssetCustody.system_user_id == filters.assigned_to_system_user_id
        )
    return query


def _custody_map(
    db: Session, source: str, asset_ids: list[UUID]
) -> dict[UUID, FieldAssetCustody]:
    if not asset_ids:
        return {}
    rows = (
        db.query(FieldAssetCustody)
        .options(
            selectinload(FieldAssetCustody.technician),
            selectinload(FieldAssetCustody.system_user),
        )
        .filter(FieldAssetCustody.asset_source == source)
        .filter(FieldAssetCustody.asset_id.in_(asset_ids))
        .filter(FieldAssetCustody.status == "issued")
        .order_by(FieldAssetCustody.issued_at.desc())
        .all()
    )
    result: dict[UUID, FieldAssetCustody] = {}
    for row in rows:
        result.setdefault(row.asset_id, row)
    return result


def _custody_fields(custody: FieldAssetCustody | None) -> dict:
    if custody is None:
        return {
            "assigned_technician_id": None,
            "assigned_system_user_id": None,
            "assigned_to": None,
        }
    user = custody.system_user
    label = None
    if user is not None:
        label = user.display_name or f"{user.first_name} {user.last_name}".strip()
    if not label and custody.technician is not None:
        label = custody.technician.title or str(custody.technician.id)
    return {
        "assigned_technician_id": custody.technician_id,
        "assigned_system_user_id": custody.system_user_id,
        "assigned_to": label,
    }


asset_inventory = AssetInventory()
