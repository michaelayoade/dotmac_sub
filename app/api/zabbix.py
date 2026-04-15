from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.zabbix import (
    ZabbixAlertsResponse,
    ZabbixHostsResponse,
    ZabbixMetricsResponse,
)
from app.services import zabbix as zabbix_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/zabbix", tags=["zabbix"])


def _zabbix_error(exc: zabbix_service.ZabbixClientError) -> HTTPException:
    if isinstance(exc, zabbix_service.ZabbixConfigurationError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Zabbix integration is not configured",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Zabbix request failed",
    )


@router.get("/hosts", response_model=ZabbixHostsResponse)
def list_zabbix_hosts(
    host_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return ZabbixHostsResponse(
            items=zabbix_service.get_hosts(host_id=host_id, limit=limit)
        )
    except zabbix_service.ZabbixClientError as exc:
        raise _zabbix_error(exc) from exc


@router.get("/metrics", response_model=ZabbixMetricsResponse)
def list_zabbix_metrics(
    host_id: str | None = Query(default=None, min_length=1, max_length=64),
    metric: str | None = Query(default=None, min_length=1, max_length=160),
    time_from: int | None = Query(default=None, ge=0),
    time_till: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    try:
        from datetime import UTC, datetime

        return ZabbixMetricsResponse(
            items=zabbix_service.get_metrics(
                host_id=host_id,
                metric=metric,
                time_from=datetime.fromtimestamp(time_from, tz=UTC)
                if time_from
                else None,
                time_till=datetime.fromtimestamp(time_till, tz=UTC)
                if time_till
                else None,
                limit=limit,
            )
        )
    except zabbix_service.ZabbixClientError as exc:
        raise _zabbix_error(exc) from exc


@router.get("/alerts", response_model=ZabbixAlertsResponse)
def list_zabbix_alerts(
    host_id: str | None = Query(default=None, min_length=1, max_length=64),
    active_only: bool = Query(default=True),
    min_priority: int | None = Query(default=None, ge=0, le=5),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return ZabbixAlertsResponse(
            items=zabbix_service.get_alerts(
                host_id=host_id,
                active_only=active_only,
                min_priority=min_priority,
                limit=limit,
            )
        )
    except zabbix_service.ZabbixClientError as exc:
        raise _zabbix_error(exc) from exc
