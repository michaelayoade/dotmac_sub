"""Web helpers for admin network topology routes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import network_topology as topology_service


def topology_page_context(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
) -> dict[str, object]:
    return {
        "graph": topology_service.list_nodes_and_edges(
            db,
            topology_group=group,
            pop_site_id=site,
            include_utilization=True,
        ),
        "form_options": topology_service.get_form_options(db),
        "selected_group": group or "",
        "selected_site": site or "",
    }


def link_form_context(
    db: Session,
    *,
    action_url: str,
    link: object | None = None,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "link": link,
        "action_url": action_url,
        "error": error,
        **topology_service.get_form_options(db),
    }


def link_edit_context(db: Session, link_id: str) -> dict[str, object]:
    return link_form_context(
        db,
        link=topology_service.topology_links.get(db, link_id),
        action_url=f"/admin/network/topology/links/{link_id}/edit",
    )


def parse_link_form(form) -> dict[str, object]:
    return {
        "source_device_id": str(form.get("source_device_id") or "").strip(),
        "source_interface_id": str(form.get("source_interface_id") or "").strip()
        or None,
        "target_device_id": str(form.get("target_device_id") or "").strip(),
        "target_interface_id": str(form.get("target_interface_id") or "").strip()
        or None,
        "link_role": str(form.get("link_role") or "unknown").strip(),
        "medium": str(form.get("medium") or "unknown").strip(),
        "capacity_bps": str(form.get("capacity_bps") or "").strip() or None,
        "bundle_key": str(form.get("bundle_key") or "").strip() or None,
        "topology_group": str(form.get("topology_group") or "").strip() or None,
        "admin_status": str(form.get("admin_status") or "enabled").strip(),
        "notes": str(form.get("notes") or "").strip() or None,
    }


def create_link(db: Session, data: dict[str, object]) -> None:
    topology_service.topology_links.create(db, data=data)


def update_link(db: Session, link_id: str, data: dict[str, object]) -> None:
    topology_service.topology_links.update(db, link_id, data=data)


def delete_link(db: Session, link_id: str) -> None:
    topology_service.topology_links.delete(db, link_id)


def get_device_interfaces(db: Session, device_id: str) -> list[dict[str, object]]:
    return topology_service.get_device_interfaces(db, device_id)


def node_summary(db: Session, device_id: str) -> dict[str, object]:
    return topology_service.node_summary(db, device_id)


def graph_data(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
) -> dict[str, object]:
    return topology_service.list_nodes_and_edges(
        db, topology_group=group, pop_site_id=site
    )
