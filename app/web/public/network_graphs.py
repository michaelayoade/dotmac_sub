"""Public bandwidth graph routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as core_devices_service

router = APIRouter(prefix="/network/graphs", tags=["public-network-graphs"])
templates = Jinja2Templates(directory="templates")


@router.get("/{token}", response_class=HTMLResponse)
def public_network_graph(request: Request, token: str, db: Session = Depends(get_db)):
    graph = core_devices_service.get_public_bandwidth_graph(db, token=token)
    if not graph:
        return templates.TemplateResponse(
            "public/network/graph.html",
            {
                "request": request,
                "graph": None,
                "rows": [],
                "error": "Graph not found or not public.",
            },
            status_code=404,
        )

    rows = []
    for source in graph.sources:
        oid = source.snmp_oid
        if not oid:
            continue
        rows.append(
            {
                "title": oid.title,
                "oid": oid.oid,
                "unit": source.value_unit,
                "color_hex": source.color_hex,
                "draw_type": source.draw_type,
            }
        )

    return templates.TemplateResponse(
        "public/network/graph.html",
        {
            "request": request,
            "graph": graph,
            "rows": rows,
            "error": None,
        },
    )
