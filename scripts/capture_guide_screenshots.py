"""Capture screenshots for the DotMac Sub user guide using Playwright."""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:8001"
OUT_DIR = Path("docs/guide_screenshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Admin credentials
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def capture(page, name: str, url: str, *, wait: int = 1500, full_page: bool = False):
    """Navigate and capture a screenshot."""
    page.goto(f"{BASE_URL}{url}", wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(wait)
    # Check if we got redirected to login
    if "/auth/login" in page.url and "/auth/login" not in url:
        print(f"  [{name}] REDIRECTED TO LOGIN — session expired?")
        return
    path = OUT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=full_page)
    print(f"  [{name}] {url} → {page.url}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        page = context.new_page()

        # ── Login ────────────────────────────────────────────
        print("==> Logging in...")
        page.goto(f"{BASE_URL}/auth/login", wait_until="networkidle")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT_DIR / "01_login.png"))
        print("  [01_login]")

        # Log in
        try:
            page.fill('input[name="username"]', ADMIN_USER)
            page.fill('input[name="password"]', ADMIN_PASS)
            page.click('button[type="submit"]')
            page.wait_for_url("**/admin/**", timeout=10000)
            page.wait_for_timeout(1500)
            # Verify we're on the dashboard, not redirected back to login
            if "/auth/login" in page.url:
                print("  LOGIN FAILED — still on login page. Check credentials.")
                browser.close()
                return
            page.screenshot(path=str(OUT_DIR / "02_dashboard.png"), full_page=True)
            print(f"  [02_dashboard] logged in at {page.url}")
        except Exception as e:
            print(f"  Login failed: {e}")
            # Try to check if we're logged in anyway
            if "/admin/" in page.url:
                print("  ...but we seem to be logged in, continuing")
            else:
                print("  Cannot proceed without login. Exiting.")
                browser.close()
                return

        # ── Admin Portal Screens ─────────────────────────────
        print("\n==> Admin Portal...")

        screens = [
            ("03_dashboard", "/admin/dashboard"),
            ("04_subscribers", "/admin/customers"),
            ("05_customers", "/admin/customers"),
            ("06_billing_overview", "/admin/billing"),
            ("07_invoices", "/admin/billing/invoices"),
            ("08_catalog_offers", "/admin/catalog/offers"),
            ("09_subscriptions", "/admin/catalog/subscriptions"),
            ("10_fup_rules", "/admin/catalog/settings"),
            ("11_network_olts", "/admin/network/olts"),
            ("12_network_onts", "/admin/network/onts"),
            ("13_network_monitoring", "/admin/network/monitoring"),
            ("14_network_topology", "/admin/network/topology"),
            ("15_network_tr069", "/admin/network/tr069"),
            ("16_network_nas", "/admin/network/nas"),
            ("17_provisioning", "/admin/provisioning"),
            ("18_gis_map", "/admin/gis"),
            ("19_reports_hub", "/admin/reports/hub"),
            ("20_reports_revenue", "/admin/reports/revenue"),
            ("21_reports_bandwidth", "/admin/reports/bandwidth"),
            ("22_reports_subscribers", "/admin/reports/subscribers"),
            ("23_notifications_templates", "/admin/notifications/templates"),
            ("24_notifications_queue", "/admin/notifications/queue"),
            ("25_system_settings", "/admin/system/settings-hub"),
            ("26_system_users", "/admin/system/users"),
            ("27_system_secrets", "/admin/system/secrets"),
            ("28_system_health", "/admin/system/health"),
            ("29_system_email", "/admin/system/email"),
            ("30_integrations", "/admin/integrations/connectors"),
            ("31_webhooks", "/admin/system/webhooks"),
            ("32_vpn_wireguard", "/admin/network/vpn"),
        ]

        for name, url in screens:
            try:
                capture(page, name, url, full_page=True)
            except Exception as e:
                print(f"  [{name}] FAILED: {e}")

        # ── Customer Portal (via impersonation) ──────────────
        print("\n==> Customer Portal...")
        capture(page, "40_portal_login", "/portal/auth/login", full_page=True)

        # Impersonate the first customer via admin endpoint
        impersonated = False
        try:
            # Navigate to a customer detail to find impersonate form
            page.goto(f"{BASE_URL}/admin/customers", wait_until="networkidle")
            page.wait_for_timeout(1000)
            # Click the first customer row
            first_row = page.locator("table tbody tr a").first
            if first_row.count():
                first_row.click()
                page.wait_for_timeout(1500)
                # Look for impersonate form/button
                impersonate_btn = page.locator('button:has-text("View as Customer"), form[action*="impersonate"] button')
                if impersonate_btn.count():
                    impersonate_btn.first.click()
                    page.wait_for_timeout(2000)
                    if "/portal/" in page.url:
                        impersonated = True
                        print(f"  Impersonated customer at {page.url}")
        except Exception as e:
            print(f"  Impersonation: {e}")

        if not impersonated:
            print("  Could not impersonate — capturing portal login page only")

        portal_screens = [
            ("41_portal_dashboard", "/portal/dashboard"),
            ("42_portal_services", "/portal/services"),
            ("43_portal_billing", "/portal/billing"),
            ("44_portal_usage", "/portal/usage"),
            ("45_portal_speedtest", "/portal/speedtest"),
            ("46_portal_support", "/portal/support"),
            ("47_portal_profile", "/portal/profile"),
        ]

        for name, url in portal_screens:
            try:
                capture(page, name, url, full_page=True)
            except Exception as e:
                print(f"  [{name}] FAILED: {e}")

        # ── Reseller Portal ──────────────────────────────────
        print("\n==> Reseller Portal...")
        capture(page, "50_reseller_login", "/reseller/auth/login", full_page=True)

        reseller_screens = [
            ("51_reseller_dashboard", "/reseller/dashboard"),
            ("52_reseller_accounts", "/reseller/accounts"),
            ("53_reseller_revenue", "/reseller/reports/revenue"),
        ]

        for name, url in reseller_screens:
            try:
                capture(page, name, url, full_page=True)
            except Exception as e:
                print(f"  [{name}] FAILED: {e}")

        # ── Mailhog (Email Testing) ─────────────────────────
        print("\n==> Infrastructure UIs...")
        try:
            page.goto("http://localhost:8025", wait_until="networkidle", timeout=10000)
            page.wait_for_timeout(1000)
            page.screenshot(path=str(OUT_DIR / "60_mailhog.png"))
            print("  [60_mailhog]")
        except Exception:
            print("  [60_mailhog] FAILED")

        try:
            page.goto("http://localhost:3000", wait_until="networkidle", timeout=10000)
            page.wait_for_timeout(1000)
            page.screenshot(path=str(OUT_DIR / "61_genieacs.png"))
            print("  [61_genieacs]")
        except Exception:
            print("  [61_genieacs] FAILED")

        browser.close()
        print(f"\n==> Done! Screenshots in {OUT_DIR}/")
        print(f"    Total: {len(list(OUT_DIR.glob('*.png')))} files")


if __name__ == "__main__":
    main()
