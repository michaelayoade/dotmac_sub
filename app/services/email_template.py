"""Branded HTML email wrapper for transactional emails.

Provides a consistent branded header, footer, and styling for all
outgoing emails (invoices, payment receipts, welcome, notifications).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Base URL for static assets in emails — must be absolute
_DEFAULT_BASE_URL = "https://subscription.dotmac.io"


def wrap_email_html(
    body_html: str,
    *,
    subject: str = "",
    base_url: str = "",
    company_name: str = "Dotmac Technologies",
    support_email: str = "support@dotmac.ng",
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

    base = base_url or _DEFAULT_BASE_URL
    safe_subject = escape(subject)
    safe_company = escape(company_name)
    safe_support = escape(support_email)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_subject}</title>
<style>
body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f8fafc; color: #1e293b; }}
.wrapper {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
.header {{ background: linear-gradient(135deg, #3b82f6 0%, #6366f1 100%); border-radius: 12px 12px 0 0; padding: 24px 32px; text-align: center; }}
.header img {{ height: 40px; width: auto; }}
.header-fallback {{ color: #ffffff; font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }}
.content {{ background: #ffffff; padding: 32px; border-left: 1px solid #e2e8f0; border-right: 1px solid #e2e8f0; }}
.footer {{ background: #f1f5f9; border-radius: 0 0 12px 12px; padding: 24px 32px; text-align: center; border: 1px solid #e2e8f0; border-top: none; }}
.footer p {{ margin: 4px 0; font-size: 12px; color: #64748b; }}
.footer a {{ color: #3b82f6; text-decoration: none; }}
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
