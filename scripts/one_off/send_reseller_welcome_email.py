#!/usr/bin/env python3
"""Send the reseller portal welcome email.

Default mode sends one preview email only. Use ``--send-all`` after approval to
send the same message to the reseller list.

Usage, inside the app container::

    python scripts/one_off/send_reseller_welcome_email.py
    python scripts/one_off/send_reseller_welcome_email.py --preview-to someone@example.com
    python scripts/one_off/send_reseller_welcome_email.py --send-all
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.services import email as email_service  # noqa: E402

SUBJECT = "Welcome - Access Your Reseller Portal"
PORTAL_URL = "https://selfcare.dotmac.io/reseller/auth/login?next=/reseller/dashboard"
MAIN_URL = "https://selfcare.dotmac.io/admin"
TEMPORARY_PASSWORD = "reseller123"
SUPPORT_EMAIL = "support@dotmac.ng"
DEFAULT_PREVIEW_TO = "s.ojo@sotmac.ng"

RESELLERS: list[tuple[str, str]] = [
    ("2dotcom", "info@2dotcom.net"),
    ("Ascomnet", "israel@ascomnetintl.com"),
    ("CareNG", "jummai.osadebe@care.org"),
    ("Cheerymoon", "cheerymoonglobal@gmail.com"),
    ("Chikalvia Integrated", "chikalviaintegrated@gmail.com"),
    ("Gems Communications", "autocentre@gemscommunications.com"),
    ("Hallowgate", "peter@hallowgate.com"),
    ("Hausba", "finance@hausba.com"),
    ("Heritage", "heritagesynergy@gmail.com"),
    ("House", "aminuumara@yahoo.com"),
    ("ISN", "accounts@isn.ng"),
    ("Matrix Global", "matrixglobalnetcom@gmail.com"),
    ("Megamore", "admin@megamore.ng"),
    ("Netflare", "netflareltd@gmail.com"),
    ("Ntel", "sunday.udosen@ntel.com.ng"),
    ("Pedery Global Concept", "dotcom404@gmail.com"),
    ("SkyPro Internet", "skyprointernet@gmail.com"),
    ("Tehilah Base Digital Ltd", "digital@tehilahbase.com"),
    ("Tremfolink", "tremfolinkng@gmail.com"),
    ("VCIT", "support@vcitng.net"),
    ("Voggnet", "info@voggnet.ng"),
    ("Vovida", "paul@vovidacommunications.com"),
    ("iDelta", "cholarink@gmail.com"),
    ("metronet", "metronet.hq@gmail.com"),
]


def _body(db, *, to_email: str, reseller_name: str | None = None) -> tuple[str, str]:
    company_name = email_service._get_company_name(db)
    logo_url = email_service._get_email_branding_logo_url(db)
    reseller_label = reseller_name.strip() if reseller_name else ""
    greeting = f"Hello {reseller_label}," if reseller_label else "Hello,"
    escaped_email = html.escape(to_email)

    intro_html = """
<p style="margin: 0 0 12px;">Welcome to the Dotmac Technologies reseller self-care portal.</p>
<p style="margin: 0;">You can manage customers, view account details, and carry out reseller admin tasks from one place.</p>
""".strip()

    details_html = f"""
<p style="font-size: 15px; margin: 0; line-height: 1.7;">
  <strong style="color: {email_service.DOTMAC_GREEN};">Portal:</strong> <span class="email-muted" style="color: #555;">Reseller Self-Care</span><br>
  <strong style="color: {email_service.DOTMAC_GREEN};">Login email:</strong> <span class="email-muted" style="color: #555;">{escaped_email}</span><br>
  <strong style="color: {email_service.DOTMAC_GREEN};">Temporary password:</strong> <span class="email-muted" style="color: #555;">{html.escape(TEMPORARY_PASSWORD)}</span>
</p>
""".strip()

    details_suffix_html = f"""
<div class="email-highlight-box" style="background-color: #f8fafc; border: 1px solid {email_service.DOTMAC_GREEN}; border-left: 5px solid {email_service.DOTMAC_RED}; border-radius: 8px; padding: 16px; margin-top: 18px;">
  <p style="margin: 0 0 10px;"><strong style="color: {email_service.DOTMAC_GREEN};">How to sign in</strong></p>
  <ol class="email-muted" style="margin: 0; padding-left: 20px; color: #555;">
    <li>Click the button above to open the reseller login page directly.</li>
    <li>If you visit <a href="{html.escape(MAIN_URL)}" style="color: {email_service.DOTMAC_GREEN}; text-decoration: none;">{html.escape(MAIN_URL)}</a> instead, click the <strong>Reseller</strong> icon on the login page.</li>
    <li>Use this email address and the temporary password shown above.</li>
    <li>After signing in, change your password from your account settings.</li>
  </ol>
</div>
""".strip()

    closing_html = """
<p style="margin: 0 0 12px;">If you need help or run into any issue, reply to this email or contact our support team.</p>
""".strip()

    body_html = email_service._render_action_email_html(
        company_name=company_name,
        logo_url=logo_url,
        title="Access Your Reseller Portal",
        accent_color=email_service._brand_accent_color(),
        greeting=greeting,
        intro_html=intro_html,
        action_url=PORTAL_URL,
        action_label="Open Reseller Portal",
        expiry_minutes=0,
        details_html=details_html,
        details_suffix_html=details_suffix_html,
        closing_html=closing_html,
        support_email=SUPPORT_EMAIL,
        secondary_color=email_service.DOTMAC_GREEN,
    )

    body_text = f"""{greeting}

Welcome to the Dotmac Technologies reseller self-care portal.

You can manage customers, view account details, and carry out reseller admin tasks from one place.

How to sign in:
1. Open the reseller portal directly: {PORTAL_URL}
2. If you visit {MAIN_URL} instead, click the Reseller icon on the login page.
3. Login email: {to_email}
4. Temporary password: {TEMPORARY_PASSWORD}

After signing in, change your password from your account settings.

If you need help or run into any issue, reply to this email or contact {SUPPORT_EMAIL}.

Thanks and welcome aboard.
{company_name} Support Team
"""
    return body_html, body_text


def _send_one(db, *, to_email: str, reseller_name: str | None = None) -> bool:
    body_html, body_text = _body(db, to_email=to_email, reseller_name=reseller_name)
    return email_service.send_email(
        db,
        to_email,
        SUBJECT,
        body_html,
        body_text,
        activity="auth_user_invite",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-to", default=DEFAULT_PREVIEW_TO)
    parser.add_argument("--send-all", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.send_all:
            failed: list[str] = []
            for reseller_name, email in RESELLERS:
                ok = _send_one(db, to_email=email, reseller_name=reseller_name)
                print(f"{'sent' if ok else 'failed'}\t{reseller_name}\t{email}")
                if not ok:
                    failed.append(email)
            print(f"done: sent={len(RESELLERS) - len(failed)} failed={len(failed)}")
            return 1 if failed else 0

        preview_to = args.preview_to.strip()
        ok = _send_one(db, to_email=preview_to)
        print(f"{'sent' if ok else 'failed'} preview to {preview_to}")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
