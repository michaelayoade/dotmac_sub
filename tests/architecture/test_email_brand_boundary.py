"""Keep transactional email a reader of the branding owner.

`customer.branding` (`app.services.brand_profiles`) owns customer-facing brand
identity and the concrete colour behind each role. `app/services/email.py` used
to resolve branding itself: two direct `SettingDomain.comms` reads for a logo, a
separate company-identity lookup for the display name, and module-level product
colour literals for the accent and link colours. That parallel resolution is
retired; these guards stop it coming back one field at a time.

Ownership boundary this file also protects: the DISPLAY brand and the LEGAL
SENDER identity stay separate. The brand snapshot must not carry `from_email`,
`from_name`, or the registered legal entity -- re-skinning a deployment changes
what a message looks like, never who is legally responsible for it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EMAIL_SERVICE = PROJECT_ROOT / "app" / "services" / "email.py"

# Structural neutrals the design-system foundation owns. Everything else in a
# rendered email must come from the resolved brand snapshot.
ALLOWED_EMAIL_HEXES = {
    "#f4f4f9",
    "#111827",
    "#555555",
    "#e2e2e2",
    "#333",
    "#ccc",
    "#666",
    "#ffffff",
    "#e5e7eb",
    "#d1d5db",
    "#374151",
    "#1f2937",
    "#f8fafc",
}

# The per-field brand plumbing the snapshot replaced. A helper that grows any of
# these back is resolving brand per call site again.
RETIRED_RENDER_PARAMETERS = {
    "company_name",
    "logo_url",
    "accent_color",
    "secondary_color",
    "support_email",
}

# Display brand only. These belong to the separately owned sender identity.
FORBIDDEN_SNAPSHOT_FIELDS = {"from_email", "from_name", "legal_name", "legal_address"}


def _module() -> ast.Module:
    return ast.parse(EMAIL_SERVICE.read_text(encoding="utf-8"), filename="email.py")


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_module()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is missing from app/services/email.py")


def test_email_does_not_read_branding_settings_directly() -> None:
    """Branding settings are the owner's inputs, not email's."""
    source = EMAIL_SERVICE.read_text(encoding="utf-8")

    assert "SettingDomain.comms" not in source
    assert "sidebar_logo_url" not in source
    assert "sidebar_logo_dark_url" not in source
    # Company identity reaches email through the branding owner's resolution.
    assert "web_system_company_info" not in source
    assert "get_company_info" not in source


def test_email_resolves_brand_through_the_owner() -> None:
    source = EMAIL_SERVICE.read_text(encoding="utf-8")

    assert "from app.services.brand_profiles import resolve_brand" in source


def test_email_declares_no_product_colour_literal() -> None:
    """Only design-system neutrals may be written as literals here."""
    source = EMAIL_SERVICE.read_text(encoding="utf-8")
    literals = {match.lower() for match in re.findall(r"#[0-9a-fA-F]{3,8}\b", source)}

    unowned = literals - ALLOWED_EMAIL_HEXES
    assert not unowned, (
        "app/services/email.py must not hardcode brand colours; "
        f"resolve them through customer.branding instead: {sorted(unowned)}"
    )


def test_action_email_renderer_takes_one_resolved_snapshot() -> None:
    """The renderer consumes a snapshot; it never re-resolves brand per field."""
    renderer = _function("_render_action_email_html")
    arguments = renderer.args
    names = {
        argument.arg
        for argument in (*arguments.args, *arguments.kwonlyargs, *arguments.posonlyargs)
    }

    assert "brand" in names
    assert not (names & RETIRED_RENDER_PARAMETERS), (
        "brand values reach the renderer as one resolved EmailBrand snapshot; "
        f"remove the per-field parameters {sorted(names & RETIRED_RENDER_PARAMETERS)}"
    )


def test_brand_snapshot_carries_display_identity_only() -> None:
    """Legal and sender identity stay outside the display-brand snapshot."""
    snapshot = next(
        node
        for node in ast.walk(_module())
        if isinstance(node, ast.ClassDef) and node.name == "EmailBrand"
    )
    fields = {
        node.target.id
        for node in snapshot.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert fields
    assert not (fields & FORBIDDEN_SNAPSHOT_FIELDS), (
        "EmailBrand is the display projection; sender and legal identity are "
        f"separately owned: {sorted(fields & FORBIDDEN_SNAPSHOT_FIELDS)}"
    )


def test_every_render_entrypoint_resolves_the_brand_exactly_once() -> None:
    """One resolution per rendered message, not one per field or per template."""
    entrypoints = (
        "render_password_reset_email",
        "send_email_verification_email",
        "render_user_invite_email",
    )
    for name in entrypoints:
        function = _function(name)
        resolutions = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_email_brand"
        ]
        assert len(resolutions) == 1, (
            f"{name} must resolve the email brand exactly once "
            f"(found {len(resolutions)})"
        )
