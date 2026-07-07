"""Web helpers for admin network topology routes."""

from __future__ import annotations

from typing import Any, cast

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


def _weather_summary(graph: dict[str, object]) -> dict[str, object]:
    nodes = cast(list[dict[str, Any]], graph.get("nodes") or [])
    edges = cast(list[dict[str, Any]], graph.get("edges") or [])
    node_counts = {"up": 0, "down": 0, "problem": 0, "unknown": 0}
    edge_counts = {"up": 0, "degraded": 0, "down": 0, "unknown": 0}
    for node in nodes:
        status = str(node.get("status") or "unknown").lower()
        if status in ("online", "up"):
            node_counts["up"] += 1
        elif status in ("offline", "down"):
            node_counts["down"] += 1
        elif status in ("degraded", "maintenance", "problem"):
            node_counts["problem"] += 1
        else:
            node_counts["unknown"] += 1
    for edge in edges:
        status = str(edge.get("status") or "unknown").lower()
        edge_counts[status if status in edge_counts else "unknown"] += 1
    hot_edges = sorted(
        [
            edge
            for edge in edges
            if edge.get("utilization_pct") is not None
            and float(edge["utilization_pct"]) >= 70
        ],
        key=lambda edge: float(edge.get("utilization_pct") or 0),
        reverse=True,
    )[:10]
    dark_nodes = [
        node
        for node in nodes
        if str(node.get("status") or "").lower() in ("down", "offline")
    ][:10]
    return {
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "hot_edges": hot_edges,
        "dark_nodes": dark_nodes,
    }


def weathermap_page_context(
    db: Session,
    *,
    group: str | None = None,
    site: str | None = None,
) -> dict[str, object]:
    graph = topology_service.list_nodes_and_edges(
        db,
        topology_group=group,
        pop_site_id=site,
        include_utilization=True,
    )
    return {
        "graph": graph,
        "weather": _weather_summary(graph),
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


def save_node_positions(db: Session, positions: list[dict]) -> dict:
    return topology_service.save_node_positions(db, positions)
