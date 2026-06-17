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
    path = CustomerPath(
        ont=SimpleNamespace(serial_number="SN-123"),
        access_device=SimpleNamespace(name="OLT-1"),
        access_device_kind="olt",
        basestation=SimpleNamespace(name="Garki", code="GARKI"),
    )
    html = _render(path)
    assert "Network Path" in html
    assert "ONT SN-123" in html
    assert "OLT &middot; OLT-1" in html or "OLT · OLT-1" in html
    assert "Garki" in html and "GARKI" in html
    assert "incomplete" not in html.lower()


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
