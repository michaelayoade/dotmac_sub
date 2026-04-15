from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.services.redis_client import get_redis
from app.services.zabbix import ZabbixClient, ZabbixClientError

logger = logging.getLogger(__name__)

HISTORY_THRESHOLD_SECONDS = 24 * 60 * 60
HOSTS_CACHE_SECONDS = 60
ITEMS_CACHE_SECONDS = 60
USAGE_CACHE_SECONDS = 30
TRENDS_CACHE_SECONDS = 120
PORTAL_USAGE_CACHE_SECONDS = 180
PORTAL_VISIBLE_SERVICE_STATUSES = [
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
]


@dataclass(frozen=True)
class CounterSample:
    item_id: str
    clock: int
    value: float


@dataclass(frozen=True)
class RatePoint:
    item_id: str
    direction: str
    timestamp: int
    bps: float
    bytes_delta: float


class _TtlCache:
    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> Any:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl_seconds, value)
        return value


class ZabbixMetricsEngine:
    def __init__(self, client: ZabbixClient | None = None) -> None:
        self.client = client or ZabbixClient.from_env()
        self._cache = _TtlCache()
        self._sample_lock = RLock()
        self._previous_samples: dict[str, CounterSample] = {}

    def load_hosts(self) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        cached = self._cache.get("hosts_all")
        if cached is not None:
            return cached
        hosts = self.client.get_hosts(limit=100000)
        host_index: dict[str, set[str]] = defaultdict(set)
        for host in hosts:
            host_id = str(host.get("hostid") or "")
            if not host_id:
                continue
            for key in self._host_index_keys(host):
                host_index[key].add(host_id)
        result = (hosts, {key: sorted(value) for key, value in host_index.items()})
        return self._cache.set("hosts_all", result, HOSTS_CACHE_SECONDS)

    def load_items(self, host_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        normalized_ids = sorted({str(host_id) for host_id in host_ids if host_id})
        if not normalized_ids:
            return {}
        cache_key = "items_all:" + ",".join(normalized_ids)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        items = self.client.get_items(host_ids=normalized_ids, metric="net.if")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            if self._is_network_counter(item):
                grouped[str(item.get("hostid") or "")].append(item)
        result = dict(grouped)
        self._cache.set("items_all", result, ITEMS_CACHE_SECONDS)
        return self._cache.set(cache_key, result, ITEMS_CACHE_SECONDS)

    def resolve_host_ids(self, identifiers: list[str]) -> list[str]:
        _hosts, host_index = self.load_hosts()
        return self._resolve_host_ids_from_index(identifiers, host_index)

    def _resolve_host_ids_from_index(
        self,
        identifiers: list[str],
        host_index: dict[str, list[str]],
    ) -> list[str]:
        resolved: set[str] = set()
        for identifier in identifiers:
            key = self._normalize_key(identifier)
            if not key:
                continue
            if key in host_index:
                for host_id in host_index[key]:
                    resolved.add(host_id)
                continue
            for index_key, host_ids in host_index.items():
                if key in index_key or index_key in key:
                    for host_id in host_ids:
                        resolved.add(host_id)
        return sorted(resolved)

    def get_normalized_metrics(
        self,
        host_ids: list[str] | None = None,
        metric: str | None = None,
        time_from: datetime | None = None,
        time_till: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        end = self._as_utc(time_till) or datetime.now(UTC)
        start = self._as_utc(time_from) or (end - timedelta(hours=1))
        selected_host_ids = sorted({str(host_id) for host_id in (host_ids or []) if host_id})
        if not selected_host_ids:
            hosts, _index = self.load_hosts()
            selected_host_ids = [str(host.get("hostid")) for host in hosts if host.get("hostid")]
        item_map = self.load_items(selected_host_ids)
        items = [
            item
            for host_items in item_map.values()
            for item in host_items
            if self._metric_matches(item, metric)
        ][:limit]
        rate_points = self._fetch_rate_points(items, start, end)
        normalized = []
        for point in rate_points:
            normalized.append(
                {
                    "metric": f"bandwidth_{point.direction}",
                    "value": round(point.bps / 1_000_000, 6),
                    "unit": "Mbps",
                    "timestamp": int(point.timestamp),
                    "source": "zabbix",
                }
            )
        return normalized

    def get_customer_usage(
        self,
        db: Session,
        subscription: Subscription,
        start_at: datetime,
        end_at: datetime,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        identifiers = self._subscription_identifiers(db, subscription)
        cache_key = (
            f"usage_{subscription.id}_{int(start_at.timestamp())}_{int(end_at.timestamp())}"
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._paginate_usage(cached, page, per_page)

        host_ids = self.resolve_host_ids(identifiers)
        if not host_ids:
            raise ZabbixClientError("No Zabbix hosts mapped for subscription")
        item_map = self.load_items(host_ids)
        items = [item for host_items in item_map.values() for item in host_items]
        rate_points = self._fetch_rate_points(items, start_at, end_at)
        usage = self._aggregate_usage(rate_points, start_at, end_at)
        self._cache.set(cache_key, usage, USAGE_CACHE_SECONDS)
        return self._paginate_usage(usage, page, per_page)

    def get_cached_customer_usage(
        self,
        subscription_id: str,
        period: str,
        page: int,
        per_page: int,
    ) -> dict[str, Any] | None:
        client = get_redis()
        if client is None:
            return None
        try:
            cached = client.get(self._portal_usage_key(subscription_id, period))
        except Exception:
            logger.info(
                "zabbix_portal_usage_cache_read_failed",
                extra={"event": "zabbix_portal_usage_cache_read_failed"},
            )
            return None
        if not cached:
            return None
        try:
            usage = json.loads(cached)
        except (TypeError, ValueError):
            return None
        if not isinstance(usage, dict) or not isinstance(usage.get("graph"), list):
            return None
        return self._paginate_usage(usage, page, per_page)

    def cache_portal_usage(
        self,
        subscription_id: str,
        period: str,
        usage: dict[str, Any],
        ttl_seconds: int = PORTAL_USAGE_CACHE_SECONDS,
    ) -> bool:
        client = get_redis()
        if client is None:
            return False
        payload = {
            "currentDownloadMbps": float(usage.get("currentDownloadMbps") or 0),
            "currentUploadMbps": float(usage.get("currentUploadMbps") or 0),
            "totalDownloadGB": float(usage.get("totalDownloadGB") or 0),
            "totalUploadGB": float(usage.get("totalUploadGB") or 0),
            "graph": [
                {
                    "timestamp": int(point.get("timestamp") or 0),
                    "download_bps": float(point.get("download_bps") or 0),
                    "upload_bps": float(point.get("upload_bps") or 0),
                    "download_mbps": float(point.get("download_mbps") or 0),
                    "upload_mbps": float(point.get("upload_mbps") or 0),
                    "download_bytes": float(point.get("download_bytes") or 0),
                    "upload_bytes": float(point.get("upload_bytes") or 0),
                }
                for point in usage.get("graph", [])
                if isinstance(point, dict)
            ],
            "cached_at": int(datetime.now(UTC).timestamp()),
            "source": "zabbix",
        }
        try:
            client.setex(
                self._portal_usage_key(subscription_id, period),
                ttl_seconds,
                json.dumps(payload, separators=(",", ":")),
            )
        except Exception:
            logger.info(
                "zabbix_portal_usage_cache_write_failed",
                extra={"event": "zabbix_portal_usage_cache_write_failed"},
            )
            return False
        return True

    def ingest_portal_usage_cache(
        self,
        db: Session,
        period: str,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, int]:
        subscriptions = (
            db.query(Subscription)
            .filter(Subscription.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES))
            .all()
        )
        return self.ingest_portal_usage_cache_for_subscriptions(
            db,
            subscriptions,
            period,
            start_at,
            end_at,
        )

    def ingest_portal_usage_cache_for_subscriptions(
        self,
        db: Session,
        subscriptions: list[Subscription],
        period: str,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, int]:
        if not subscriptions:
            return {"subscriptions": 0, "mapped": 0, "cached": 0, "points": 0}

        _hosts, host_index = self.load_hosts()
        subscription_hosts: dict[str, list[str]] = {}
        all_host_ids: set[str] = set()
        for subscription in subscriptions:
            identifiers = self._subscription_identifiers(db, subscription)
            host_ids = self._resolve_host_ids_from_index(identifiers, host_index)
            if not host_ids:
                continue
            subscription_id = str(subscription.id)
            subscription_hosts[subscription_id] = host_ids
            for host_id in host_ids:
                all_host_ids.add(host_id)
        if not all_host_ids:
            return {
                "subscriptions": len(subscriptions),
                "mapped": 0,
                "cached": 0,
                "points": 0,
            }

        item_map = self.load_items(sorted(all_host_ids))
        all_items = [item for items in item_map.values() for item in items]
        if not all_items:
            return {
                "subscriptions": len(subscriptions),
                "mapped": len(subscription_hosts),
                "cached": 0,
                "points": 0,
            }

        item_by_id = {str(item.get("itemid")): item for item in all_items}
        with self._sample_lock:
            for item_id in item_by_id:
                self._previous_samples.pop(item_id, None)
        rate_points = self._fetch_rate_points(all_items, start_at, end_at)
        points_by_host: dict[str, list[RatePoint]] = defaultdict(list)
        for point in rate_points:
            item = item_by_id.get(point.item_id)
            if not item:
                continue
            host_id = str(item.get("hostid") or "")
            if host_id:
                points_by_host[host_id].append(point)

        cached_count = 0
        for subscription_id, host_ids in subscription_hosts.items():
            points = [
                point
                for host_id in host_ids
                for point in points_by_host.get(host_id, [])
            ]
            if not points:
                continue
            usage = self._aggregate_usage(points, start_at, end_at)
            if self.cache_portal_usage(subscription_id, period, usage):
                cached_count += 1
        return {
            "subscriptions": len(subscriptions),
            "mapped": len(subscription_hosts),
            "cached": cached_count,
            "points": len(rate_points),
        }

    def get_bandwidth_series(
        self,
        db: Session,
        subscription: Subscription,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, Any]:
        usage = self.get_customer_usage(db, subscription, start_at, end_at, 1, 10000)
        graph = usage["graph"]
        return {
            "data": [
                {
                    "timestamp": datetime.fromtimestamp(point["timestamp"], tz=UTC),
                    "rx_bps": point["download_bps"],
                    "tx_bps": point["upload_bps"],
                }
                for point in graph
            ],
            "total": len(graph),
            "source": "zabbix",
        }

    def get_bandwidth_stats(
        self,
        db: Session,
        subscription: Subscription,
        period: str,
    ) -> dict[str, Any]:
        period_map = {
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }
        end = datetime.now(UTC)
        start = end - period_map.get(period, timedelta(hours=24))
        usage = self.get_customer_usage(db, subscription, start, end, 1, 10000)
        graph = usage["graph"]
        latest = graph[-1] if graph else {}
        return {
            "current_rx_bps": float(latest.get("download_bps") or 0),
            "current_tx_bps": float(latest.get("upload_bps") or 0),
            "peak_rx_bps": max((float(p["download_bps"]) for p in graph), default=0),
            "peak_tx_bps": max((float(p["upload_bps"]) for p in graph), default=0),
            "total_rx_bytes": int(float(usage["totalDownloadGB"]) * (1024**3)),
            "total_tx_bytes": int(float(usage["totalUploadGB"]) * (1024**3)),
            "sample_count": len(graph),
            "source": "zabbix",
        }

    def _fetch_rate_points(
        self,
        items: list[dict[str, Any]],
        start_at: datetime,
        end_at: datetime,
    ) -> list[RatePoint]:
        if not items:
            return []
        start_ts = int(start_at.timestamp())
        end_ts = int(end_at.timestamp())
        duration = max(0, end_ts - start_ts)
        if duration <= HISTORY_THRESHOLD_SECONDS:
            return self._history_rate_points(items, start_ts, end_ts)
        cache_key = "trends:" + ",".join(sorted(str(i.get("itemid")) for i in items))
        cache_key += f":{start_ts}:{end_ts}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        rate_points = self._trend_rate_points(items, start_ts, end_ts)
        return self._cache.set(cache_key, rate_points, TRENDS_CACHE_SECONDS)

    def _history_rate_points(
        self,
        items: list[dict[str, Any]],
        start_ts: int,
        end_ts: int,
    ) -> list[RatePoint]:
        by_type: dict[int, list[str]] = defaultdict(list)
        item_by_id = {str(item.get("itemid")): item for item in items}
        for item in items:
            item_id = str(item.get("itemid") or "")
            if not item_id:
                continue
            try:
                value_type = int(item.get("value_type") or 3)
            except (TypeError, ValueError):
                value_type = 3
            by_type[value_type].append(item_id)

        rows: list[dict[str, Any]] = []
        for value_type, item_ids in by_type.items():
            rows.extend(
                self.client.get_history(
                    item_ids=item_ids,
                    history_type=value_type,
                    time_from=start_ts,
                    time_till=end_ts,
                )
            )
        return self._rates_from_rows(rows, item_by_id, "value")

    def _trend_rate_points(
        self,
        items: list[dict[str, Any]],
        start_ts: int,
        end_ts: int,
    ) -> list[RatePoint]:
        item_ids = [str(item.get("itemid")) for item in items if item.get("itemid")]
        item_by_id = {str(item.get("itemid")): item for item in items}
        rows = self.client.get_trends(item_ids=item_ids, time_from=start_ts, time_till=end_ts)
        return self._rates_from_rows(rows, item_by_id, "value_avg")

    def _rates_from_rows(
        self,
        rows: list[dict[str, Any]],
        item_by_id: dict[str, dict[str, Any]],
        value_field: str,
    ) -> list[RatePoint]:
        grouped: dict[str, list[CounterSample]] = defaultdict(list)
        for row in rows:
            item_id = str(row.get("itemid") or "")
            if item_id not in item_by_id:
                continue
            try:
                clock = int(row.get("clock") or 0)
                value = float(row.get(value_field) or 0)
            except (TypeError, ValueError):
                continue
            if clock > 0:
                grouped[item_id].append(CounterSample(item_id=item_id, clock=clock, value=value))

        points: list[RatePoint] = []
        for item_id, samples in grouped.items():
            samples = sorted(samples, key=lambda item: item.clock)
            item = item_by_id[item_id]
            direction = self._direction(item)
            previous = self._previous_samples.get(item_id)
            for sample in samples:
                if previous:
                    point = self._rate_point(item, sample, previous, direction)
                    if point:
                        points.append(point)
                previous = sample
            if previous:
                with self._sample_lock:
                    self._previous_samples[item_id] = previous
        return sorted(points, key=lambda point: point.timestamp)

    def _rate_point(
        self,
        item: dict[str, Any],
        current: CounterSample,
        previous: CounterSample,
        direction: str,
    ) -> RatePoint | None:
        delta = current.value - previous.value
        seconds = current.clock - previous.clock
        if seconds <= 0 or delta < 0:
            return None
        bytes_delta = delta if self._counter_is_bytes(item) else delta / 8.0
        bps = (bytes_delta * 8.0) / seconds
        return RatePoint(
            item_id=current.item_id,
            direction=direction,
            timestamp=current.clock,
            bps=max(0.0, bps),
            bytes_delta=max(0.0, bytes_delta),
        )

    def _aggregate_usage(
        self,
        points: list[RatePoint],
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, Any]:
        buckets: dict[int, dict[str, float]] = defaultdict(
            lambda: {
                "download_bps": 0.0,
                "upload_bps": 0.0,
                "download_bytes": 0.0,
                "upload_bytes": 0.0,
            }
        )
        for point in points:
            bucket = point.timestamp
            if point.direction == "out":
                buckets[bucket]["upload_bps"] += point.bps
                buckets[bucket]["upload_bytes"] += point.bytes_delta
            else:
                buckets[bucket]["download_bps"] += point.bps
                buckets[bucket]["download_bytes"] += point.bytes_delta
        graph = []
        for timestamp in sorted(buckets):
            row = buckets[timestamp]
            graph.append(
                {
                    "timestamp": timestamp,
                    "download_bps": row["download_bps"],
                    "upload_bps": row["upload_bps"],
                    "download_mbps": row["download_bps"] / 1_000_000,
                    "upload_mbps": row["upload_bps"] / 1_000_000,
                    "download_bytes": row["download_bytes"],
                    "upload_bytes": row["upload_bytes"],
                }
            )
        total_download_bytes = sum(row["download_bytes"] for row in buckets.values())
        total_upload_bytes = sum(row["upload_bytes"] for row in buckets.values())
        latest = graph[-1] if graph else {}
        return {
            "currentDownloadMbps": float(latest.get("download_mbps") or 0),
            "currentUploadMbps": float(latest.get("upload_mbps") or 0),
            "totalDownloadGB": total_download_bytes / (1024**3),
            "totalUploadGB": total_upload_bytes / (1024**3),
            "graph": graph,
            "period_start": start_at,
            "period_end": end_at,
        }

    def _paginate_usage(self, usage: dict[str, Any], page: int, per_page: int) -> dict[str, Any]:
        by_day: dict[datetime, float] = defaultdict(float)
        for point in usage["graph"]:
            day = datetime.fromtimestamp(point["timestamp"], tz=UTC).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            by_day[day] += float(point.get("download_bytes") or 0)
            by_day[day] += float(point.get("upload_bytes") or 0)

        records = [
            SimpleNamespace(
                recorded_at=day,
                usage_type="Zabbix Bandwidth",
                amount=total_bytes / (1024**3),
                usage_amount=total_bytes / (1024**3),
                unit="GB",
                description=f"Counter-derived usage for {day.date().isoformat()}",
            )
            for day, total_bytes in sorted(by_day.items(), reverse=True)
        ]
        total = len(records)
        start = (page - 1) * per_page
        end = start + per_page
        total_days = max(1, total)
        avg_download = (
            sum(float(point["download_bps"]) for point in usage["graph"]) / len(usage["graph"])
            if usage["graph"]
            else 0.0
        )
        avg_upload = (
            sum(float(point["upload_bps"]) for point in usage["graph"]) / len(usage["graph"])
            if usage["graph"]
            else 0.0
        )
        usage_summary = {
            "average_daily_usage_gb": (
                (float(usage["totalDownloadGB"]) + float(usage["totalUploadGB"])) / total_days
            ),
            "average_speed_mbps": (avg_download + avg_upload) / 1_000_000,
            "average_download_mbps": avg_download / 1_000_000,
            "average_upload_mbps": avg_upload / 1_000_000,
        }
        return {
            **usage,
            "usage_records": records[start:end],
            "total": total,
            "usage_summary": usage_summary,
            "source": "zabbix",
        }

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _normalize_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9@._-]+", "", str(value or "").strip().lower())

    @staticmethod
    def _portal_usage_key(subscription_id: str, period: str) -> str:
        normalized_period = str(period or "current").strip().lower()
        if normalized_period not in {"current", "last"}:
            normalized_period = "current"
        return f"zabbix:usage:{subscription_id}:{normalized_period}"

    def _host_index_keys(self, host: dict[str, Any]) -> set[str]:
        values: set[str] = set()
        for field in ("host", "name"):
            values.add(str(host.get(field) or ""))
        for group in host.get("groups") or []:
            if isinstance(group, dict):
                values.add(str(group.get("name") or ""))
        for tag in host.get("tags") or []:
            if isinstance(tag, dict):
                values.add(str(tag.get("value") or ""))
        inventory = host.get("inventory")
        if isinstance(inventory, dict):
            for value in inventory.values():
                if isinstance(value, str):
                    values.add(value)
        return {key for key in (self._normalize_key(value) for value in values) if key}

    @staticmethod
    def _is_network_counter(item: dict[str, Any]) -> bool:
        key = str(item.get("key_") or "").lower()
        name = str(item.get("name") or "").lower()
        return "net.if" in key or "octets" in key or "traffic" in name

    def _metric_matches(self, item: dict[str, Any], metric: str | None) -> bool:
        if not metric:
            return True
        needle = self._normalize_key(metric)
        return needle in self._normalize_key(item.get("name")) or needle in self._normalize_key(
            item.get("key_")
        )

    @staticmethod
    def _direction(item: dict[str, Any]) -> str:
        key = str(item.get("key_") or "").lower()
        name = str(item.get("name") or "").lower()
        if ".out" in key or "out[" in key or "sent" in name or "upload" in name:
            return "out"
        return "in"

    @staticmethod
    def _counter_is_bytes(item: dict[str, Any]) -> bool:
        units = str(item.get("units") or "").strip().lower()
        return units not in {"b", "bps", "bit", "bits"}

    def _subscription_identifiers(
        self,
        db: Session,
        subscription: Subscription,
    ) -> list[str]:
        identifiers = [
            str(subscription.id),
            str(subscription.subscriber_id),
            subscription.login or "",
            subscription.ipv4_address or "",
            subscription.mac_address or "",
        ]
        if subscription.subscriber:
            subscriber = subscription.subscriber
            identifiers.extend(
                [
                    str(getattr(subscriber, "id", "") or ""),
                    str(getattr(subscriber, "display_name", "") or ""),
                    str(getattr(subscriber, "name", "") or ""),
                    str(getattr(subscriber, "email", "") or ""),
                    str(getattr(subscriber, "phone", "") or ""),
                ]
            )
        credentials = (
            db.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == subscription.subscriber_id,
                AccessCredential.is_active == True,
            )
            .all()
        )
        identifiers.extend(credential.username for credential in credentials)
        return [item for item in identifiers if self._normalize_key(item)]


_ENGINE: ZabbixMetricsEngine | None = None


def get_zabbix_engine() -> ZabbixMetricsEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ZabbixMetricsEngine()
    return _ENGINE
