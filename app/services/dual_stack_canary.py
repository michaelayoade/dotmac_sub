"""Read-only evidence gate for a Huawei TR-181 dual-stack canary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.network import OntAssignment, OntUnit
from app.models.ont_observation import OntObservation
from app.models.usage import RadiusAccountingSession
from app.services.ipv6_pd import active_delegated_prefix_for_subscription
from app.services.network.ont_desired_config import desired_config
from app.services.radius import read_external_radius_rows_for_username


@dataclass(frozen=True)
class CanaryCheck:
    name: str
    passed: bool
    evidence: Any = None


def _radius_pd_values(rows: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for source in rows:
        if not source.get("available"):
            continue
        for row in source.get("radreply") or []:
            if str(row.get("attribute") or "") == "Delegated-IPv6-Prefix":
                values.add(str(row.get("value") or ""))
    return values


def evaluate_dual_stack_canary(
    db: Session,
    ont_id: str,
    *,
    run_probes: bool = False,
    ipv6_probe_target: str = "2606:4700:4700::1111",
    dns_probe_target: str = "one.one.one.one",
) -> dict[str, Any]:
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return {"passed": False, "checks": [asdict(CanaryCheck("ont_exists", False))]}

    assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True))
        .order_by(OntAssignment.assigned_at.desc())
    ).first()
    subscriber_id = assignment.subscriber_id if assignment else None
    subscription = (
        db.get(Subscription, assignment.subscription_id)
        if assignment and assignment.subscription_id
        else None
    )
    subscription_binding_ambiguous = False
    if subscription is None and subscriber_id:
        candidates = list(
            db.scalars(
                select(Subscription)
                .where(
                    Subscription.subscriber_id == subscriber_id,
                    Subscription.status == SubscriptionStatus.active,
                )
                .order_by(Subscription.created_at.desc())
                .limit(2)
            ).all()
        )
        subscription = candidates[0] if len(candidates) == 1 else None
        subscription_binding_ambiguous = len(candidates) > 1
    credential = (
        db.scalars(
            select(AccessCredential)
            .where(
                AccessCredential.subscriber_id == subscriber_id,
                AccessCredential.subscription_id == subscription.id,
                AccessCredential.is_active.is_(True),
            )
            .order_by(AccessCredential.created_at.desc())
        ).first()
        if subscriber_id and subscription
        else None
    )
    if credential is None and subscriber_id:
        legacy_credentials = list(
            db.scalars(
                select(AccessCredential)
                .where(
                    AccessCredential.subscriber_id == subscriber_id,
                    AccessCredential.subscription_id.is_(None),
                    AccessCredential.is_active.is_(True),
                )
                .limit(2)
            ).all()
        )
        credential = legacy_credentials[0] if len(legacy_credentials) == 1 else None
    observation = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
    ).first()
    desired = desired_config(ont)
    ip_protocol = str((desired.get("wan") or {}).get("ip_protocol") or "ipv4")
    pd = active_delegated_prefix_for_subscription(
        db,
        subscription.id if subscription else None,
        subscriber_id=subscriber_id,
    )
    radius_rows = (
        read_external_radius_rows_for_username(db, credential.username)
        if credential
        else []
    )
    radius_pd = _radius_pd_values(radius_rows)
    accounting = (
        db.scalars(
            select(RadiusAccountingSession)
            .where(
                RadiusAccountingSession.subscription_id == subscription.id,
                RadiusAccountingSession.session_end.is_(None),
            )
            .order_by(RadiusAccountingSession.last_update_at.desc())
        ).first()
        if subscription
        else None
    )

    checks = [
        CanaryCheck("ont_active", bool(ont.is_active), ont.serial_number),
        CanaryCheck("active_assignment", assignment is not None),
        CanaryCheck(
            "catalog_subscription_bound",
            subscription is not None and not subscription_binding_ambiguous,
            str(subscription.id) if subscription else "ambiguous_or_missing",
        ),
        CanaryCheck("tr181", ont.tr069_data_model == "Device", ont.tr069_data_model),
        CanaryCheck("desired_dual_stack", ip_protocol == "dual_stack", ip_protocol),
        CanaryCheck(
            "acs_ipv6_enabled",
            bool(observation and observation.acs_observed_ipv6_enabled),
        ),
        CanaryCheck(
            "acs_dhcpv6_enabled",
            bool(observation and observation.acs_observed_dhcpv6_enabled),
        ),
        CanaryCheck(
            "acs_requests_prefix",
            bool(observation and observation.acs_observed_dhcpv6_request_prefixes),
        ),
        CanaryCheck(
            "acs_router_advertisement",
            bool(observation and observation.acs_observed_ra_enabled),
        ),
        CanaryCheck("pd_assigned", bool(pd), pd),
        CanaryCheck(
            "radius_pd_matches", bool(pd and pd in radius_pd), sorted(radius_pd)
        ),
        CanaryCheck(
            "accounting_pd_matches",
            bool(accounting and pd and accounting.delegated_ipv6_prefix == pd),
            accounting.delegated_ipv6_prefix if accounting else None,
        ),
    ]

    if run_probes:
        from app.services.network.ont_action_device import run_ping_diagnostic

        ipv6_probe = run_ping_diagnostic(db, str(ont.id), ipv6_probe_target, count=3)
        dns_probe = run_ping_diagnostic(db, str(ont.id), dns_probe_target, count=3)
        checks.extend(
            [
                CanaryCheck(
                    "ipv6_traffic_probe", ipv6_probe.success, ipv6_probe.message
                ),
                CanaryCheck("dns_probe", dns_probe.success, dns_probe.message),
            ]
        )

    rendered = [asdict(check) for check in checks]
    return {
        "passed": all(check.passed for check in checks),
        "ont_id": str(ont.id),
        "serial_number": ont.serial_number,
        "radius_username": credential.username if credential else None,
        "checks": rendered,
    }
