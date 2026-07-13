"""Branded HTML email wrapper for transactional emails.

Provides a consistent branded header, footer, and styling for all
outgoing emails (invoices, payment receipts, welcome, notifications).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.services.branding_config import get_brand

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
_FULL_HTML_DOCUMENT_RE = re.compile(r"<!doctype\b|<html\b", re.IGNORECASE)
DOTMAC_RED = "#FF0000"
DOTMAC_GREEN = "#008000"
DOTMAC_WHITE = "#F4F4F9"


def looks_like_html(body: str | None) -> bool:
    return bool(_HTML_TAG_RE.search(body or ""))


def looks_like_full_html_document(body: str | None) -> bool:
    return bool(_FULL_HTML_DOCUMENT_RE.search(body or ""))


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "br",
        "div",
        "li",
        "ol",
        "p",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(body: str | None) -> str:
    """Return readable text for an HTML email body."""
    if not body:
        return ""
    if not looks_like_html(body):
        return body
    parser = _HTMLTextExtractor()
    parser.feed(body)
    parser.close()
    return parser.text()


def render_email_bodies(
    body: str,
    *,
    subject: str = "",
    base_url: str = "",
    company_name: str | None = None,
    support_email: str | None = None,
    brand: Mapping[str, object] | None = None,
) -> tuple[str, str | None]:
    """Return ``(body_html, body_text)`` for an outgoing email.

    Plain-text input is escaped, converted to paragraphs (blank line =
    paragraph break), and wrapped in the branded template; the original text
    is kept as the text/plain part. Input that already contains HTML is
    wrapped as-is and converted to a readable text/plain part.
    """
    if looks_like_full_html_document(body):
        return body, html_to_text(body)

    if looks_like_html(body):
        html = wrap_email_html(
            body,
            subject=subject,
            base_url=base_url,
            company_name=company_name,
            support_email=support_email,
            brand=brand,
        )
        return html, html_to_text(body)

    paragraphs = [
        f'<p style="margin: 0 0 16px; font-size: 14px; line-height: 1.6;">'
        f"{escape(para).replace(chr(10), '<br>')}</p>"
        for para in re.split(r"\n\s*\n", body or "")
        if para.strip()
    ]
    html = wrap_email_html(
        "\n".join(paragraphs),
        subject=subject,
        base_url=base_url,
        company_name=company_name,
        support_email=support_email,
        brand=brand,
    )
    return html, body


def _asset_url(base_url: str, path: str) -> str:
    return urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))


def wrap_email_html(
    body_html: str,
    *,
    subject: str = "",
    base_url: str = "",
    company_name: str | None = None,
    support_email: str | None = None,
    brand: Mapping[str, object] | None = None,
) -> str:
    """Wrap email body HTML in a branded template with header and footer.

    Args:
        body_html: The email body content (HTML).
        subject: Email subject (shown as preheader).
        base_url: Base URL for absolute asset links.
        company_name: Company name for footer.
        support_email: Support email for footer.

    Returns:
        Full HTML email document with branded header/footer.
    """
    from html import escape

    resolved_brand = dict(brand) if brand is not None else get_brand()
    base = base_url or str(resolved_brand["app_url"])
    primary = str(resolved_brand.get("primary_color") or DOTMAC_RED)
    secondary = str(resolved_brand.get("secondary_color") or DOTMAC_GREEN)
    configured_logo = str(resolved_brand.get("logo_url") or "").strip()
    logo_url = (
        _asset_url(base, configured_logo)
        if configured_logo
        else _asset_url(base, "/static/branding/favicon/icon-192.png")
    )
    safe_subject = escape(subject)
    safe_company = escape(
        company_name if company_name is not None else str(resolved_brand["legal_name"])
    )
    safe_support = escape(
        support_email
        if support_email is not None
        else str(resolved_brand["support_email"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{safe_subject}</title>
<style>
body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: {DOTMAC_WHITE}; color: #1f2937; }}
.wrapper {{ max-width: 600px; margin: 0 auto; padding: 24px 20px; }}
.header {{ background: {DOTMAC_WHITE}; padding: 0 0 22px; text-align: center; border-bottom: 3px solid {primary}; }}
.header img {{ max-height: 64px; max-width: 160px; width: auto; height: auto; }}
.header-fallback {{ color: {primary}; font-size: 20px; font-weight: 700; margin-top: 8px; }}
.content {{ background: {DOTMAC_WHITE}; padding: 30px 0; color: #374151; }}
.content a {{ color: {secondary}; text-decoration: none; }}
.footer {{ background: {DOTMAC_WHITE}; padding: 20px 0 0; text-align: center; border-top: 1px solid #e5e7eb; }}
.footer p {{ margin: 4px 0; font-size: 12px; color: #6b7280; }}
.footer a {{ color: {secondary}; text-decoration: none; }}
.brand-accent {{ color: {primary}; }}
@media (prefers-color-scheme: dark) {{
  body {{ background-color: #111827 !important; color: #e5e7eb !important; }}
  .wrapper, .header, .content, .footer {{ background-color: #111827 !important; }}
  .content {{ color: #d1d5db !important; }}
  .footer {{ border-top-color: #374151 !important; }}
  .footer p {{ color: #d1d5db !important; }}
}}
</style>
</head>
<body>
<div class="wrapper">
    <div class="header">
        <img src="{escape(logo_url)}" alt="{safe_company} logo">
        <div class="header-fallback" style="margin-top: 8px;">{safe_company}</div>
    </div>

    <div class="content">
        {body_html}
    </div>

    <div class="footer">
        <p>&copy; {safe_company}</p>
        <p>Need help? <a href="mailto:{safe_support}">{safe_support}</a></p>
    </div>
</div>
</body>
</html>"""
