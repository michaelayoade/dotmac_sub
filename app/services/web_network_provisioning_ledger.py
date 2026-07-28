"""Provisioning ledger page data — a projection over the provisioning owners.

Consolidates the in-flight provisioning inventory into one archetype-D ledger
with a facet per record type, each sourced from its CRUD list owner in
``provisioning_managers``:
  - orders: ServiceOrder (owner: service_orders)
  - runs: ProvisioningRun (owner: provisioning_runs)
  - tasks: ProvisioningTask (owner: provisioning_tasks)
  - appointments: InstallAppointment (owner: install_appointments)

Entity lifecycle status tone comes from the server-owned presentations. The
cross-vendor ControlPlanePhase convergence lens is a separate (phase-2) triage,
per NETWORK_PROVISIONING_BUILD_SPEC.md. Read-only projection; mutations stay on
the provisioning action owners.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.services.provisioning_managers import (
    install_appointments,
    provisioning_runs,
    provisioning_tasks,
    service_orders,
)
from app.services.status_presentation import (
    appointment_status_presentation,
    provisioning_run_status_presentation,
    provisioning_task_status_presentation,
    service_order_status_presentation,
)

_LIMIT = 500

# facet order + labels
FACETS: tuple[tuple[str, str], ...] = (
    ("orders", "Service orders"),
    ("runs", "Provisioning runs"),
    ("tasks", "Tasks"),
    ("appointments", "Appointments"),
)
_VALID = {key for key, _ in FACETS}


def _dt(value: datetime | None) -> str:
    return value.strftime("%b %d, %H:%M") if value else "—"


def _order_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Order", "order"),
        ("Type", "type"),
        ("Status", "__status"),
        ("Created", "created"),
    ]
    rows = [
        {
            "id": str(o.id),
            "order": str(o.id)[:8],
            "type": getattr(o.order_type, "value", None) or "—",
            "status": service_order_status_presentation(o.status),
            "created": _dt(o.created_at),
        }
        for o in service_orders.list(db, limit=_LIMIT)
    ]
    return columns, rows


def _run_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Run", "run"),
        ("Status", "__status"),
        ("Started", "started"),
        ("Error", "error"),
    ]
    rows = []
    for r in provisioning_runs.list(db, None, None, "created_at", "desc", _LIMIT, 0):
        err = r.error_message or ""
        rows.append(
            {
                "id": str(r.id),
                "run": str(r.id)[:8],
                "status": provisioning_run_status_presentation(r.status),
                "started": _dt(r.started_at),
                "error": (err[:60] + "…") if len(err) > 60 else (err or "—"),
            }
        )
    return columns, rows


def _task_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Task", "name"),
        ("Status", "__status"),
        ("Assigned", "assigned"),
        ("Created", "created"),
    ]
    rows = [
        {
            "id": str(t.id),
            "name": t.name,
            "status": provisioning_task_status_presentation(t.status),
            "assigned": t.assigned_to or "—",
            "created": _dt(t.created_at),
        }
        for t in provisioning_tasks.list(
            db, None, None, "created_at", "desc", _LIMIT, 0
        )
    ]
    return columns, rows


def _appointment_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Appointment", "appt"),
        ("Status", "__status"),
        ("Technician", "tech"),
        ("Scheduled", "scheduled"),
    ]
    rows = []
    for a in install_appointments.list(db, None, None, "created_at", "desc", _LIMIT, 0):
        tech = a.technician or ("Self-install" if a.is_self_install else "—")
        rows.append(
            {
                "id": str(a.id),
                "appt": str(a.id)[:8],
                "status": appointment_status_presentation(a.status),
                "tech": tech,
                "scheduled": _dt(a.scheduled_start),
            }
        )
    return columns, rows


_DISPATCH = {
    "orders": _order_rows,
    "runs": _run_rows,
    "tasks": _task_rows,
    "appointments": _appointment_rows,
}


def provisioning_ledger_data(db: Session, facet: str = "orders") -> dict:
    """Return the ledger page data for one provisioning facet (from its owner)."""
    facet = facet if facet in _VALID else "orders"
    columns, rows = _DISPATCH[facet](db)
    return {
        "facet": facet,
        "facet_label": dict(FACETS)[facet],
        "facets": [{"key": k, "label": lbl} for k, lbl in FACETS],
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "detail_base": "",
    }
