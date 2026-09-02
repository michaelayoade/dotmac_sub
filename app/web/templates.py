"""Shared Jinja2 templates configuration for web routes.

This module provides a centralized Jinja2Templates instance with custom filters
registered. All web route modules should import templates from here to ensure
consistent filter availability.

Usage:
    from app.web.templates import templates

    @router.get("/")
    def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})
"""

from __future__ import annotations

import html
import re

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

# Create shared templates instance
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Custom Jinja Filters
# ---------------------------------------------------------------------------


def format_speed(kbps: int | None) -> str:
    """Format speed in kbps to human-readable format.

    Args:
        kbps: Speed in kilobits per second

    Returns:
        Formatted speed string (e.g., "100 Mbps", "1 Gbps")

    Examples:
        >>> format_speed(1000000)
        '1 Gbps'
        >>> format_speed(100000)
        '100 Mbps'
        >>> format_speed(512)
        '512 Kbps'
    """
    if kbps is None:
        return "N/A"
    if kbps >= 1_000_000:
        gbps = kbps / 1_000_000
        # Show decimal only if needed
        if gbps == int(gbps):
            return f"{int(gbps)} Gbps"
        return f"{gbps:.1f} Gbps"
    elif kbps >= 1_000:
        mbps = kbps / 1_000
        if mbps == int(mbps):
            return f"{int(mbps)} Mbps"
        return f"{mbps:.1f} Mbps"
    return f"{kbps} Kbps"


def format_speed_pair(download_kbps: int | None, upload_kbps: int | None) -> str:
    """Format download/upload speed pair.

    Args:
        download_kbps: Download speed in kilobits per second
        upload_kbps: Upload speed in kilobits per second

    Returns:
        Formatted speed pair string (e.g., "100/50 Mbps")
    """
    if download_kbps is None and upload_kbps is None:
        return "N/A"
    down = format_speed(download_kbps) if download_kbps else "N/A"
    up = format_speed(upload_kbps) if upload_kbps else "N/A"
    return f"{down} / {up}"


def vlan_label(vlan) -> str:
    """Format VLAN for display with purpose label.

    Args:
        vlan: VLAN object with tag, name, purpose attributes

    Returns:
        Formatted VLAN label (e.g., "100 - Internet (WAN)")
    """
    if vlan is None:
        return "None"
    parts = [str(vlan.tag)]
    if hasattr(vlan, "purpose") and vlan.purpose:
        purpose_str = vlan.purpose.value.replace("_", " ").title()
        parts.append(f"- {purpose_str}")
    if hasattr(vlan, "name") and vlan.name:
        parts.append(f"({vlan.name})")
    return " ".join(parts)


_MANAGER_AI_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _format_manager_ai_inline(value: str) -> str:
    return _MANAGER_AI_BOLD.sub(r"<strong>\1</strong>", value)


def render_manager_ai_answer(value: str | None) -> Markup:
    """Render the Manager AI's limited Markdown-like response safely.

    Model output is escaped before the small presentation vocabulary is applied.
    This supports the headings, bold labels, paragraphs, and bullet lists used
    by the Manager AI prompt without allowing model output to inject HTML.
    """

    if not value:
        return Markup("")

    rendered: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush_paragraph() -> None:
        if paragraph:
            rendered.append(f'<p class="mb-4 last:mb-0">{"<br>".join(paragraph)}</p>')
            paragraph.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            rendered.append("</ul>")
            in_list = False

    for raw_line in str(value).splitlines():
        line = html.escape(raw_line.strip(), quote=False)
        if line.startswith("- ") or line.startswith("* "):
            flush_paragraph()
            if not in_list:
                rendered.append('<ul class="mb-4 list-disc space-y-1 pl-5">')
                in_list = True
            rendered.append(f"<li>{_format_manager_ai_inline(line[2:])}</li>")
        elif not line:
            flush_paragraph()
            close_list()
        else:
            close_list()
            paragraph.append(_format_manager_ai_inline(line))

    flush_paragraph()
    close_list()
    # Every dynamic fragment is escaped above; only the static tags/classes are
    # marked safe for Jinja.
    return Markup("".join(rendered))  # noqa: S704


# Register filters
templates.env.filters["format_speed"] = format_speed
templates.env.filters["format_speed_pair"] = format_speed_pair
templates.env.filters["vlan_label"] = vlan_label
templates.env.filters["render_manager_ai_answer"] = render_manager_ai_answer


__all__ = [
    "templates",
    "format_speed",
    "format_speed_pair",
    "render_manager_ai_answer",
    "vlan_label",
]
