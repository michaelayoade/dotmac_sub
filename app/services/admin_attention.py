"""Owner of the admin overview's "needs attention" exception feed.

The dashboard leads with actionable exceptions. This module owns which
exceptions appear, their severity, thresholds, ordering, and semantic tone —
decisions that previously lived inline in the dashboard web service *and* were
extended again inside the template. Inputs are facts already computed by their
domain read owners; this function only decides attention-worthiness.

Severity vocabulary: critical > major > warning > info. Each item carries a
semantic ``tone`` so templates map tone → colour and never re-derive meaning.
"""

from __future__ import annotations

_SEVERITY_RANK = {"critical": 0, "major": 1, "warning": 2, "info": 3}
_SEVERITY_TONE = {
    "critical": "negative",
    "major": "negative",
    "warning": "warning",
    "info": "info",
}

# Only show offline ONTs when the count is operationally significant.
ONT_OFFLINE_ATTENTION_THRESHOLD = 5


def _item(label: str, href: str, severity: str, domain: str) -> dict:
    return {
        "label": label,
        "href": href,
        "severity": severity,
        "domain": domain,
        "tone": _SEVERITY_TONE.get(severity, "info"),
    }


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def build_attention_items(
    *,
    net_stats: dict,
    overdue_amount: float,
    suspended_count: int,
    pending_orders: int,
    ont_summary: dict,
    unconfigured_ont_count: int,
    pending_location_requests: int,
    pon_outage_count: int,
    infrastructure_alerts: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Decide the overview's attention items from owner-computed facts.

    Returns ``(attention_items, network_attention_items)`` ordered by severity
    then insertion order. Domain visibility filtering (RBAC) stays with the
    renderer; inclusion and severity are decided here.
    """
    items: list[dict] = []
    network_items: list[dict] = []

    def _add(item: dict, *, network: bool = False) -> None:
        items.append(item)
        if network:
            network_items.append(item)

    critical = int(net_stats.get("alarms_critical") or 0)
    if critical > 0:
        _add(
            _item(
                f"{critical} critical {_plural(critical, 'alarm')}",
                "/admin/network/alarms",
                "critical",
                "network",
            ),
            network=True,
        )
    major = int(net_stats.get("alarms_major") or 0)
    if major > 0:
        _add(
            _item(
                f"{major} major {_plural(major, 'alarm')}",
                "/admin/network/alarms",
                "major",
                "network",
            ),
            network=True,
        )
    if pon_outage_count > 0:
        _add(
            _item(
                f"{pon_outage_count} PON {_plural(pon_outage_count, 'port')} down",
                "/admin/network/pon-interfaces?status=down",
                "critical",
                "network",
            ),
            network=True,
        )
    infra = infrastructure_alerts or {}
    infra_total = int(infra.get("total") or 0)
    if infra_total > 0:
        infra_critical = int(infra.get("critical") or 0)
        _add(
            _item(
                f"{infra_total} infrastructure "
                f"{_plural(infra_total, 'alert')} ({infra_critical} critical)",
                "/admin/alerts?category=infrastructure&status=open",
                "critical" if infra_critical else "warning",
                "network",
            ),
            network=True,
        )
    offline = int(net_stats.get("offline_count") or 0)
    if offline > 0:
        _add(
            _item(
                f"{offline} {_plural(offline, 'device')} offline",
                "/admin/network/monitoring",
                "warning",
                "network",
            ),
            network=True,
        )
    if overdue_amount > 0:
        _add(
            _item(
                f"₦{overdue_amount:,.0f} overdue receivables",
                "/admin/billing",
                "warning",
                "billing",
            )
        )
    if suspended_count > 0:
        _add(
            _item(
                f"{suspended_count} suspended {_plural(suspended_count, 'account')}",
                "/admin/customers",
                "info",
                "customers",
            )
        )
    if pending_orders > 0:
        _add(
            _item(
                f"{pending_orders} pending service {_plural(pending_orders, 'order')}",
                "/admin/provisioning",
                "info",
                "provisioning",
            )
        )

    ont_low_signal = int(ont_summary.get("low_signal") or 0)
    if ont_low_signal > 0:
        _add(
            _item(
                f"{ont_low_signal} {_plural(ont_low_signal, 'ONT')} with low signal",
                "/admin/network/onts?view=diagnostics&signal_quality=warning"
                "&order_by=signal&order_dir=asc",
                "warning",
                "network",
            ),
            network=True,
        )
    ont_offline = int(ont_summary.get("offline") or 0)
    if ont_offline > ONT_OFFLINE_ATTENTION_THRESHOLD:
        _add(
            _item(
                f"{ont_offline} {_plural(ont_offline, 'ONT')} offline",
                "/admin/network/onts?view=list&olt_status=offline",
                "warning",
                "network",
            ),
            network=True,
        )
    if unconfigured_ont_count > 0:
        _add(
            _item(
                f"{unconfigured_ont_count} unconfigured "
                f"{_plural(unconfigured_ont_count, 'ONT')} awaiting authorization",
                "/admin/network/onts?view=unconfigured",
                "info",
                "network",
            ),
            network=True,
        )
    if pending_location_requests > 0:
        _add(
            _item(
                f"{pending_location_requests} pending pin "
                f"{_plural(pending_location_requests, 'correction')}",
                "/admin/gis?tab=customer-requests&status=pending",
                "info",
                "customers",
            )
        )

    def _rank(item: dict) -> int:
        return _SEVERITY_RANK.get(item["severity"], 99)

    items.sort(key=_rank)
    network_items.sort(key=_rank)
    return items, network_items
