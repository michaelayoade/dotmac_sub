"""Branded HTML email wrapper for transactional emails.

Provides a consistent branded header, footer, and styling for all
outgoing emails (invoices, payment receipts, welcome, notifications).
"""

from __future__ import annotations

import logging
import re
from html import escape

from app.services.branding_config import get_brand

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")


def looks_like_html(body: str | None) -> bool:
    return bool(_HTML_TAG_RE.search(body or ""))


def render_email_bodies(
    body: str,
    *,
    subject: str = "",
    base_url: str = "",
    company_name: str | None = None,
    support_email: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(body_html, body_text)`` for an outgoing email.

    Plain-text input is escaped, converted to paragraphs (blank line =
    paragraph break), and wrapped in the branded template; the original text
    is kept as the text/plain part. Input that already contains HTML is
    wrapped as-is with no text part.
    """
    if looks_like_html(body):
        html = wrap_email_html(
            body,
            subject=subject,
            base_url=base_url,
            company_name=company_name,
            support_email=support_email,
        )
        return html, None

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
    )
    return html, body


def _darken(hex_color: str, factor: float = 0.78) -> str:
    """Return a darker shade of a #rrggbb colour, for an on-brand gradient end
    stop. Falls back to the input if it isn't a 6-digit hex."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def wrap_email_html(
    body_html: str,
    *,
    subject: str = "",
    base_url: str = "",
    company_name: str | None = None,
    support_email: str | None = None,
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

    brand = get_brand()
    base = base_url or brand["app_url"]
    primary = brand["primary_color"]
    primary_dark = _darken(primary)
    safe_subject = escape(subject)
    safe_company = escape(
        company_name if company_name is not None else brand["legal_name"]
    )
    safe_support = escape(
        support_email if support_email is not None else brand["support_email"]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_subject}</title>
<style>
body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f8fafc; color: #1e293b; }}
.wrapper {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
.header {{ background: linear-gradient(135deg, {primary} 0%, {primary_dark} 100%); border-radius: 12px 12px 0 0; padding: 24px 32px; text-align: center; }}
.header img {{ height: 40px; width: auto; }}
.header-fallback {{ color: #ffffff; font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }}
.content {{ background: #ffffff; padding: 32px; border-left: 1px solid #e2e8f0; border-right: 1px solid #e2e8f0; }}
.footer {{ background: #f1f5f9; border-radius: 0 0 12px 12px; padding: 24px 32px; text-align: center; border: 1px solid #e2e8f0; border-top: none; }}
.footer p {{ margin: 4px 0; font-size: 12px; color: #64748b; }}
.footer a {{ color: {primary}; text-decoration: none; }}
</style>
</head>
<body>
<div class="wrapper">
    <!-- Header with branded banner -->
    <div class="header">
        <img src="{base}/static/illustrations/email-header.png" alt="" style="max-width: 100%; height: 40px; object-fit: contain;">
        <div class="header-fallback" style="margin-top: 8px;">{safe_company}</div>
    </div>

    <!-- Email body -->
    <div class="content">
        {body_html}
    </div>

    <!-- Footer -->
    <div class="footer">
        <p>&copy; {safe_company}</p>
        <p>Need help? <a href="mailto:{safe_support}">{safe_support}</a></p>
    </div>
</div>
</body>
</html>"""
