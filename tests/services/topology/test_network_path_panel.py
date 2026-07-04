"""Network Path panel renders from a CustomerPath (Phase 1, Task 7)."""

from __future__ import annotations

from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.services.topology.customer_path import CustomerPath

PANEL = "admin/catalog/_network_path_panel.html"


def _env():
    return Environment(loader=FileSystemLoader("templates"), autoescape=True)


def _render(path: CustomerPath) -> str:
    return _env().get_template(PANEL).render(network_path=path)


def test_detail_template_compiles():
    # The full detail template (which includes the panel) must still parse.
    _env().get_template("admin/catalog/subscription_detail.html")


def test_renders_full_fiber_chain():
    from datetime import UTC, datetime

    path = CustomerPath(
        ont=SimpleNamespace(serial_number="SN-123"),
        access_device=SimpleNamespace(name="OLT-1"),
        access_device_kind="olt",
        node=SimpleNamespace(
            live_status="up", live_status_at=datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
        ),
        basestation=SimpleNamespace(name="Garki", code="GARKI"),
    )
    html = _render(path)
    assert "Network Path" in html
    assert "ONT SN-123" in html
    assert "OLT &middot; OLT-1" in html or "OLT · OLT-1" in html
    assert "Garki" in html and "GARKI" in html
    assert "incomplete" not in html.lower()
    # up status -> green dot + tooltip
    assert "bg-emerald-500" in html
    assert "Status: Up" in html


def test_renders_physical_fiber_plant_chain():
    path = CustomerPath(
        ont=SimpleNamespace(serial_number="SN-FULL"),
        splitter_port=SimpleNamespace(port_number=12),
        splitter=SimpleNamespace(name="SPL-A"),
        fdh=SimpleNamespace(code="FDH-A12", name="Alpha 12"),
        pon_port=SimpleNamespace(name="0/1/2"),
        access_device=SimpleNamespace(name="OLT-1"),
        access_device_kind="olt",
    )
    html = _render(path)
    assert "Fiber Plant" in html
    assert "ONT SN-FULL" in html
    assert "Splitter Port 12" in html
    assert "Splitter SPL-A" in html
    assert "FDH FDH-A12" in html
    assert "PON 0/1/2" in html
    assert "OLT OLT-1" in html


def test_renders_partial_fiber_plant_as_not_mapped():
    path = CustomerPath(
        ont=SimpleNamespace(serial_number="SN-PARTIAL"),
        access_device=SimpleNamespace(name="OLT-Partial"),
        access_device_kind="olt",
    )
    html = _render(path)
    assert "Fiber Plant" in html
    assert "SN-PARTIAL" in html
    assert "not mapped" in html


def test_renders_upstream_chain():
    path = CustomerPath(
        access_device=SimpleNamespace(name="SPDC Access"),
        access_device_kind="nas",
        node=SimpleNamespace(live_status="up", live_status_at=None),
        basestation=SimpleNamespace(name="SPDC", code=None),
        upstream_chain=[
            SimpleNamespace(name="Agg-1", live_status="up"),
            SimpleNamespace(name="Core-1", live_status="down"),
        ],
    )
    html = _render(path)
    assert "Upstream" in html
    assert "Agg-1" in html and "Core-1" in html
    assert "bg-rose-500" in html  # Core-1 down dot
    assert "not mapped yet" not in html


def test_upstream_not_mapped_hint_when_node_but_no_chain():
    path = CustomerPath(
        access_device=SimpleNamespace(name="N"),
        access_device_kind="nas",
        node=SimpleNamespace(live_status="up", live_status_at=None),
        basestation=SimpleNamespace(name="BTS", code=None),
        upstream_chain=[],
    )
    html = _render(path)
    assert "Upstream not mapped yet" in html


def test_status_dot_colors():
    def dot(status):
        return _render(
            CustomerPath(
                access_device=SimpleNamespace(name="N"),
                access_device_kind="nas",
                node=SimpleNamespace(live_status=status, live_status_at=None),
            )
        )

    assert "bg-rose-500" in dot("down")
    assert "bg-amber-500" in dot("problem")
    # unknown / no node -> grey
    assert "bg-slate-300" in dot(None)


def test_renders_nas_without_ont():
    path = CustomerPath(
        access_device=SimpleNamespace(name="NAS-1"),
        access_device_kind="nas",
        basestation=SimpleNamespace(name="Lekki", code=None),
    )
    html = _render(path)
    assert "NAS" in html and "NAS-1" in html
    assert "ONT" not in html.split("Network Path")[1].split("NAS")[0]  # no ONT chip
    assert "Lekki" in html


def test_renders_gap_message():
    html = _render(CustomerPath(gap="no_ont"))
    assert "provisioning incomplete" in html.lower()
