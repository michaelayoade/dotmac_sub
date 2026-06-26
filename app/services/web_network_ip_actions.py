"""Web action helpers for admin IP-management routes."""

from __future__ import annotations

import csv
import io
import ipaddress
import uuid
from dataclasses import dataclass
from urllib.parse import quote_plus

from fastapi import Request
from sqlalchemy import func

from app.services import network as network_service
from app.services import web_network_ip as ip_service
from app.services.audit_helpers import (
    build_audit_activities,
    build_audit_activities_for_types,
    log_audit_event,
)


def _safe_return_to(value: str | None, default: str) -> str:
    """Constrain a form-supplied ``return_to`` to a same-origin admin path.

    The value is seeded from the Referer header and round-tripped through a hidden
    form field, so without this an attacker-controlled value would be an open
    redirect. Only a relative ``/admin/...`` path is accepted; anything absolute,
    protocol-relative (``//evil``), or scheme-bearing falls back to ``default``.
    """
    candidate = (value or "").strip()
    if (
        candidate.startswith("/admin/")
        and not candidate.startswith("//")
        and "://" not in candidate
        and "\\" not in candidate
    ):
        return candidate
    return default


@dataclass
class IpWebActionResult:
    success: bool
    form_context: dict[str, object] | None = None
    redirect_url: str | None = None
    error: str | None = None
    not_found_message: str | None = None


def _actor_id_from_request(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if current_user:
        subscriber_id = current_user.get("subscriber_id")
        if subscriber_id:
            return str(subscriber_id)
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, dict) and auth.get("sub"):
        return str(auth.get("sub"))
    return None


def _log_ip_audit_event(
    db,
    request: Request,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    metadata: dict[str, object] | None,
) -> None:
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
    )


def activity_for_types(db, entity_types: list[str], *, limit: int = 5):
    return build_audit_activities_for_types(db, entity_types, limit=limit)


def activity_for_entity(db, entity_type: str, entity_id: str):
    return build_audit_activities(db, entity_type, entity_id)


def reconcile_ipv4_pool_memberships_redirect(request: Request, db) -> str:
    result = ip_service.reconcile_ipv4_pool_memberships(db)
    notice = (
        "Reconciled IPv4 address pool membership: "
        f"{result['updated']} updated, {result['unchanged']} unchanged."
    )
    warning_parts: list[str] = []
    if result["unmatched"]:
        warning_parts.append(
            f"{result['unmatched']} address(es) did not match any configured pool"
        )
    if result["conflicts"]:
        warning_parts.append(
            f"{result['conflicts']} address(es) matched multiple pools"
        )
    if result["invalid"]:
        warning_parts.append(f"{result['invalid']} invalid address row(s)")

    _log_ip_audit_event(
        db,
        request,
        action="reconcile",
        entity_type="ip_pool",
        entity_id=None,
        metadata=result,
    )

    redirect_url = f"/admin/network/ip-management?notice={quote_plus(notice)}"
    if warning_parts:
        redirect_url += f"&warning={quote_plus('. '.join(warning_parts))}"
    return redirect_url


def ip_block_form_context(
    db,
    block_data: dict[str, object],
    *,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "block": block_data,
        "pools": ip_service.list_active_ip_pools(db),
        "action_url": "/admin/network/ip-management/blocks",
    }
    if error:
        context["error"] = error
    return context


def create_ip_block_from_form(request: Request, db, form) -> IpWebActionResult:
    block_data = ip_service.parse_ip_block_form(form)
    error = ip_service.validate_ip_block_values(block_data)
    if error:
        return IpWebActionResult(
            success=False,
            form_context=ip_block_form_context(db, block_data, error=error),
            error=error,
        )

    block, error = ip_service.create_ip_block(db, block_data)
    if not error and block is not None:
        _log_ip_audit_event(
            db,
            request,
            action="create",
            entity_type="ip_block",
            entity_id=str(block.id),
            metadata={"cidr": block.cidr, "pool_id": str(block.pool_id)},
        )
        return IpWebActionResult(
            success=True, redirect_url="/admin/network/ip-management"
        )

    return IpWebActionResult(
        success=False,
        form_context=ip_block_form_context(
            db,
            block_data,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def ip_pool_form_context(
    db,
    pool_data: dict[str, object],
    *,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "pool": pool_data,
        "action_url": action_url,
        "olt_devices": ip_service.list_active_olts(db),
        "vlans": ip_service.list_active_vlans(db),
    }
    if error:
        context["error"] = error
    return context


def create_ip_pool_from_form(request: Request, db, form) -> IpWebActionResult:
    pool_values = ip_service.parse_ip_pool_form(form)
    error = ip_service.validate_ip_pool_values(pool_values)
    pool_data = ip_service.pool_form_snapshot(pool_values)
    action_url = "/admin/network/ip-management/pools"
    if error:
        return IpWebActionResult(
            success=False,
            form_context=ip_pool_form_context(
                db,
                pool_data,
                action_url=action_url,
                error=error,
            ),
            error=error,
        )

    pool, error = ip_service.create_ip_pool(db, pool_values)
    if not error and pool is not None:
        _log_ip_audit_event(
            db,
            request,
            action="create",
            entity_type="ip_pool",
            entity_id=str(pool.id),
            metadata={"name": pool.name, "cidr": pool.cidr},
        )
        return IpWebActionResult(
            success=True, redirect_url="/admin/network/ip-management"
        )

    return IpWebActionResult(
        success=False,
        form_context=ip_pool_form_context(
            db,
            pool_data,
            action_url=action_url,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def update_ip_pool_from_form(
    request: Request, db, *, pool_id: str, form
) -> IpWebActionResult:
    pool = ip_service.get_ip_pool_for_edit(db, pool_id=pool_id)
    if pool is None:
        return IpWebActionResult(success=False, not_found_message="IP Pool not found")

    pool_values = ip_service.parse_ip_pool_form(form)
    error = ip_service.validate_ip_pool_values(pool_values)
    pool_data = ip_service.pool_form_snapshot(pool_values, pool_id=str(pool.id))
    action_url = f"/admin/network/ip-management/pools/{pool_id}"
    if error:
        return IpWebActionResult(
            success=False,
            form_context=ip_pool_form_context(
                db,
                pool_data,
                action_url=action_url,
                error=error,
            ),
            error=error,
        )

    _, changes, error = ip_service.update_ip_pool(
        db, pool_id=pool_id, values=pool_values
    )
    if not error:
        _log_ip_audit_event(
            db,
            request,
            action="update",
            entity_type="ip_pool",
            entity_id=str(pool_id),
            metadata={"changes": changes} if changes else None,
        )
        return IpWebActionResult(
            success=True,
            redirect_url=f"/admin/network/ip-management/pools/{pool_id}",
        )

    return IpWebActionResult(
        success=False,
        form_context=ip_pool_form_context(
            db,
            pool_data,
            action_url=action_url,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def assign_ipv4_address_from_form(request: Request, db, form) -> IpWebActionResult:
    pool_id = str(form.get("pool_id") or "").strip()
    block_id = str(form.get("block_id") or "").strip() or None
    ip_address = str(form.get("ip_address") or "").strip()
    subscriber_id = str(form.get("subscriber_id") or "").strip()
    subscription_id = str(form.get("subscription_id") or "").strip() or None
    return_to = _safe_return_to(
        str(form.get("return_to") or ""),
        f"/admin/network/ip-management/ipv4-networks/{pool_id}",
    )

    state = ip_service.build_ipv4_assignment_form_data(
        db,
        pool_id=pool_id,
        ip_address=ip_address,
        block_id=block_id,
    )
    if state is None:
        return IpWebActionResult(
            success=False,
            not_found_message="IPv4 address not found for this range",
        )

    try:
        result = ip_service.assign_ipv4_address(
            db,
            pool_id=pool_id,
            ip_address=ip_address,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            block_id=block_id,
        )
    except Exception as exc:
        return IpWebActionResult(
            success=False,
            form_context={
                **state,
                "return_to": return_to,
                "action_url": "/admin/network/ip-management/ipv4-assign",
                "error": str(exc),
                "subscriber_id": subscriber_id or state.get("subscriber_id"),
                "subscription_id": subscription_id or state.get("subscription_id"),
            },
            error=str(exc),
        )

    # The ip_assignments row is patched in place on reassignment (one row per
    # address, enforced by uq_ip_assignments_ipv4_address_id). Ownership
    # history therefore lives in the audit log: record the prior owner so the
    # transfer is reconstructable.
    _log_ip_audit_event(
        db,
        request,
        action="reassign" if result.get("reassigned") else "assign",
        entity_type="ip_assignment",
        entity_id=str(getattr(result.get("assignment"), "id", "") or ""),
        metadata={
            "pool_id": pool_id,
            "block_id": block_id,
            "ip_address": ip_address,
            "subscriber_id": subscriber_id,
            "subscription_id": subscription_id,
            "old_subscriber_id": result.get("previous_subscriber_id"),
            "old_subscription_id": result.get("previous_subscription_id"),
        },
    )
    return IpWebActionResult(success=True, redirect_url=return_to)


def release_ipv4_address_from_form(request: Request, db, form) -> IpWebActionResult:
    """Release (unassign) an IPv4 address from its current subscriber.

    Soft-deletes the assignment row (is_active=False) via the service layer,
    which also clears the subscription's cached ipv4_address. The row is kept
    so a later re-assignment reactivates it in place — a fresh insert would
    collide with uq_ip_assignments_ipv4_address_id. The released owner is
    recorded in the audit log as the ownership history.
    """
    pool_id = str(form.get("pool_id") or "").strip()
    block_id = str(form.get("block_id") or "").strip() or None
    ip_address = str(form.get("ip_address") or "").strip()
    return_to = _safe_return_to(
        str(form.get("return_to") or ""),
        f"/admin/network/ip-management/ipv4-networks/{pool_id}",
    )

    state = ip_service.build_ipv4_assignment_form_data(
        db,
        pool_id=pool_id,
        ip_address=ip_address,
        block_id=block_id,
    )
    if state is None:
        return IpWebActionResult(
            success=False,
            not_found_message="IPv4 address not found for this range",
        )

    assignment = state.get("assignment")
    if assignment is None:
        # Nothing assigned — releasing is a no-op; return to the range view.
        return IpWebActionResult(success=True, redirect_url=return_to)

    assignment_id = str(getattr(assignment, "id", ""))
    old_subscriber_id = str(getattr(assignment, "subscriber_id", "") or "") or None
    old_subscription_id = str(getattr(assignment, "subscription_id", "") or "") or None

    try:
        network_service.ip_assignments.delete(db, assignment_id)
    except Exception as exc:
        return IpWebActionResult(success=False, error=str(exc))

    _log_ip_audit_event(
        db,
        request,
        action="release",
        entity_type="ip_assignment",
        entity_id=assignment_id,
        metadata={
            "pool_id": pool_id,
            "block_id": block_id,
            "ip_address": ip_address,
            "old_subscriber_id": old_subscriber_id,
            "old_subscription_id": old_subscription_id,
        },
    )
    return IpWebActionResult(success=True, redirect_url=return_to)


def _parse_bulk_assign_csv(csv_text: str) -> list[dict[str, str]]:
    """Parse bulk-assign CSV into lowercased-key rows.

    Recognised columns: ip_address (or ip), subscriber (or subscriber_id /
    account / email), subscription (or subscription_id, optional).
    """
    text = (csv_text or "").strip()
    if not text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append(
            {str(k or "").strip().lower(): str(v or "").strip() for k, v in raw.items()}
        )
    return rows


def _resolve_bulk_subscriber(db, identifier: str):
    """Resolve a CSV subscriber identifier to a Subscriber (id, account#, email)."""
    from app.models.subscriber import Subscriber

    ident = (identifier or "").strip()
    if not ident:
        return None
    try:
        subscriber = db.get(Subscriber, uuid.UUID(ident))
        if subscriber is not None:
            return subscriber
    except (ValueError, AttributeError):
        pass
    subscriber = db.query(Subscriber).filter(Subscriber.account_number == ident).first()
    if subscriber is not None:
        return subscriber
    return (
        db.query(Subscriber)
        .filter(func.lower(Subscriber.email) == ident.lower())
        .first()
    )


def _find_ipv4_pool_for_address(db, ip: str):
    """Return the active IPv4 pool whose CIDR contains ``ip``, else None."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    for pool in ip_service.list_active_ip_pools(db):
        cidr = str(getattr(pool, "cidr", "") or "")
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if network.version == 4 and addr in network:
            return pool
    return None


def _subscriber_label(subscriber) -> str:
    name = " ".join(
        part
        for part in [
            getattr(subscriber, "first_name", "") or "",
            getattr(subscriber, "last_name", "") or "",
        ]
        if part
    ).strip()
    account = getattr(subscriber, "account_number", "") or ""
    if name and account:
        return f"{name} ({account})"
    return name or account or str(getattr(subscriber, "id", ""))


def bulk_assign_ipv4(db, rows: list[dict[str, str]]) -> dict[str, object]:
    """Assign each CSV row, reusing single-IP semantics (assign_ipv4_address).

    Each row is independent: a failure records an error and moves on so a
    bad row never blocks the rest. Returns a summary plus an ``audit`` list
    the caller emits as audit events (kept separate so this stays testable
    without a Request).
    """
    assigned = 0
    reassigned = 0
    errors: list[dict[str, object]] = []
    details: list[dict[str, object]] = []
    audit: list[dict[str, object]] = []

    for index, row in enumerate(rows, start=2):  # header is line 1
        ip = (row.get("ip_address") or row.get("ip") or "").strip()
        subscriber_ident = (
            row.get("subscriber")
            or row.get("subscriber_id")
            or row.get("account")
            or row.get("email")
            or ""
        ).strip()
        subscription_ident = (
            row.get("subscription") or row.get("subscription_id") or ""
        ).strip()

        if not ip or not subscriber_ident:
            errors.append(
                {
                    "line": index,
                    "ip": ip,
                    "subscriber": subscriber_ident,
                    "error": "ip_address and subscriber are required",
                }
            )
            continue

        subscriber = _resolve_bulk_subscriber(db, subscriber_ident)
        if subscriber is None:
            errors.append(
                {
                    "line": index,
                    "ip": ip,
                    "subscriber": subscriber_ident,
                    "error": "Subscriber not found",
                }
            )
            continue

        pool = _find_ipv4_pool_for_address(db, ip)
        if pool is None:
            errors.append(
                {
                    "line": index,
                    "ip": ip,
                    "subscriber": subscriber_ident,
                    "error": "No active IPv4 pool contains this address",
                }
            )
            continue

        subscription_id = None
        if subscription_ident:
            try:
                subscription_id = str(uuid.UUID(subscription_ident))
            except ValueError:
                errors.append(
                    {
                        "line": index,
                        "ip": ip,
                        "subscriber": subscriber_ident,
                        "error": "subscription must be a UUID",
                    }
                )
                continue

        try:
            result = ip_service.assign_ipv4_address(
                db,
                pool_id=str(pool.id),
                ip_address=ip,
                subscriber_id=str(subscriber.id),
                subscription_id=subscription_id,
            )
        except Exception as exc:  # noqa: BLE001 - row-level isolation
            db.rollback()
            errors.append(
                {
                    "line": index,
                    "ip": ip,
                    "subscriber": subscriber_ident,
                    "error": str(exc),
                }
            )
            continue

        was_reassign = bool(result.get("reassigned"))
        if was_reassign:
            reassigned += 1
        else:
            assigned += 1
        details.append(
            {
                "line": index,
                "ip": ip,
                "subscriber": _subscriber_label(subscriber),
                "action": "reassigned" if was_reassign else "assigned",
            }
        )
        audit.append(
            {
                "action": "reassign" if was_reassign else "assign",
                "entity_id": str(getattr(result.get("assignment"), "id", "") or ""),
                "metadata": {
                    "pool_id": str(pool.id),
                    "ip_address": ip,
                    "subscriber_id": str(subscriber.id),
                    "subscription_id": subscription_id,
                    "old_subscriber_id": result.get("previous_subscriber_id"),
                    "old_subscription_id": result.get("previous_subscription_id"),
                    "source": "bulk_csv",
                },
            }
        )

    return {
        "assigned": assigned,
        "reassigned": reassigned,
        "errors": errors,
        "details": details,
        "total_rows": len(rows),
        "audit": audit,
    }


def bulk_assign_ipv4_from_form(request: Request, db, form) -> IpWebActionResult:
    csv_text = str(form.get("csv_data") or "")
    summary = bulk_assign_ipv4(db, _parse_bulk_assign_csv(csv_text))
    audit_events = summary.pop("audit")
    if isinstance(audit_events, list):
        for event in audit_events:
            if not isinstance(event, dict):
                continue
            _log_ip_audit_event(
                db,
                request,
                action=event["action"],
                entity_type="ip_assignment",
                entity_id=event["entity_id"],
                metadata=event["metadata"],
            )
    return IpWebActionResult(
        success=True,
        form_context={"csv_data": csv_text, "result": summary, "error": None},
    )


def ipv6_network_form_values(form) -> dict[str, object]:
    return {
        "title": str(form.get("title") or ""),
        "network": str(form.get("network") or ""),
        "prefix_length": str(form.get("prefix_length") or "64"),
        "comment": str(form.get("comment") or ""),
        "location": str(form.get("location") or ""),
        "category": str(form.get("category") or "Dev"),
        "network_type": str(form.get("network_type") or "EndNet"),
        "usage_type": str(form.get("usage_type") or "Static"),
        "router": str(form.get("router") or ""),
        "gateway": str(form.get("gateway") or ""),
        "dns_primary": str(form.get("dns_primary") or ""),
        "dns_secondary": str(form.get("dns_secondary") or ""),
        "is_active": form.get("is_active") == "true",
    }


def create_ipv6_network_from_form(request: Request, db, form) -> IpWebActionResult:
    pool_values = ip_service.parse_ipv6_network_form(form)
    error = ip_service.validate_ip_pool_values(pool_values)
    form_values = ipv6_network_form_values(form)
    if error:
        return IpWebActionResult(
            success=False,
            form_context={"form_values": form_values, "error": error},
            error=error,
        )

    pool, error = ip_service.create_ip_pool(db, pool_values)
    if not error and pool is not None:
        _log_ip_audit_event(
            db,
            request,
            action="create",
            entity_type="ip_pool",
            entity_id=str(pool.id),
            metadata={"name": pool.name, "cidr": pool.cidr, "ip_version": "ipv6"},
        )
        return IpWebActionResult(
            success=True,
            redirect_url="/admin/network/ip-management/ipv6-networks",
        )

    return IpWebActionResult(
        success=False,
        form_context={
            "form_values": form_values,
            "error": error or "Please correct the highlighted fields.",
        },
        error=error,
    )
