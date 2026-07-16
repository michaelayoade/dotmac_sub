"""Guardrails for the public landing page (public.landing).

The `/` page is public and unauthenticated: its route must carry no customer or
operational data, and the template must render only brand-owned values and
static task navigation — no fabricated volume/SLA claims.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ROUTE = _REPO / "app" / "web_home.py"
_TEMPLATE = _REPO / "templates" / "index.html"

# Modules a public unauthenticated route must never read from.
_FORBIDDEN_IMPORT_PREFIXES = (
    "app.services.subscriber",
    "app.services.billing",
    "app.services.network",
    "app.models",
)


def _route_imports() -> list[str]:
    tree = ast.parse(_ROUTE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_landing_route_reads_no_domain_data():
    """Boundary: the public route resolves brand only — no domain reads."""
    imports = _route_imports()
    offenders = [
        name
        for name in imports
        if any(name.startswith(prefix) for prefix in _FORBIDDEN_IMPORT_PREFIXES)
    ]
    assert not offenders, (
        "public landing route imports domain modules (unauthenticated page must "
        f"carry no customer/operational data): {offenders}"
    )
    assert any("brand_profiles" in name for name in imports), (
        "landing route should resolve the canonical brand owner"
    )


def test_landing_template_has_no_fabricated_claims():
    """No volume/SLA/support-hours claims without a named owner."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    for fabricated in ("99.9%", "10K+", "50+", "24/7", "Uptime SLA"):
        assert fabricated not in source, (
            f"fabricated claim {fabricated!r} reappeared in the landing template; "
            "derived values need a named owner (relevance test)"
        )


def test_landing_template_contract_anatomy():
    """Task cards, brand-owned contact, and demoted staff/reseller links."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    # Three task cards route to the customer portal.
    for destination in ("/portal/services", "/portal/billing", "/portal/support"):
        assert destination in source, f"task card destination missing: {destination}"
    # Support strip renders brand-owned contact, conditionally.
    assert "landing_brand.support_phone" in source
    assert "landing_brand.support_email" in source
    # Staff and reseller sign-in demoted to footer utility links.
    footer = source.split("<footer", 1)[1]
    assert '"/auth/login"' in footer, "staff sign-in should live in the footer"
    assert '"/reseller"' in footer, "reseller sign-in should live in the footer"
    # ...and not promoted anywhere above the footer.
    body = source.split("<footer", 1)[0]
    assert '"/admin"' not in body, (
        "admin entry must not be promoted on the landing page"
    )
    assert '"/reseller"' not in body, (
        "reseller entry must not be promoted above the footer"
    )
