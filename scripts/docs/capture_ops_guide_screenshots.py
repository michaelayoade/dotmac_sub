"""Capture screenshots for the Admin Operations Guide with highlighted areas."""

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:8001"
OUT_DIR = Path("docs/guide_screenshots/ops")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def capture(page, name, url, *, wait=2000, full_page=True, highlight_selectors=None):
    """Navigate, optionally highlight elements, and capture."""
    page.goto(f"{BASE_URL}{url}", wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(wait)
    if "/auth/login" in page.url and "/auth/login" not in url:
        print(f"  [{name}] REDIRECTED — skipping")
        return

    # Add red border highlights to specified selectors
    if highlight_selectors:
        for sel in highlight_selectors:
            try:
                page.evaluate(f"""
                    document.querySelectorAll('{sel}').forEach(el => {{
                        el.style.outline = '3px solid #ef4444';
                        el.style.outlineOffset = '2px';
                        el.style.borderRadius = '8px';
                    }});
                """)
            except Exception:
                pass
        page.wait_for_timeout(300)

    page.screenshot(path=str(OUT_DIR / f"{name}.png"), full_page=full_page)
    print(f"  [{name}] {url}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2
        )
        page = context.new_page()

        # Login
        print("==> Logging in...")
        page.goto(f"{BASE_URL}/auth/login", wait_until="networkidle")
        page.fill('input[name="username"]', ADMIN_USER)
        page.fill('input[name="password"]', ADMIN_PASS)
        page.click('button[type="submit"]')
        page.wait_for_url("**/admin/**", timeout=10000)
        page.wait_for_timeout(1500)
        print(f"  Logged in at {page.url}")

        # ── Configuration Screens ────────────────────────────
        print("\n==> Configuration screens...")

        configs = [
            # Company & Branding
            ("ops_01_company_info", "/admin/system/company-info"),
            ("ops_02_branding", "/admin/system/branding"),
            # Settings Hub
            ("ops_03_settings_hub", "/admin/system/settings-hub"),
            ("ops_04_settings_billing", "/admin/system/settings-hub?category=billing"),
            # Billing Config
            ("ops_05_billing_config", "/admin/system/config/billing"),
            ("ops_06_tax_config", "/admin/system/config/tax"),
            ("ops_07_payment_methods", "/admin/system/config/payment-methods"),
            ("ops_08_finance_automation", "/admin/system/config/finance-automation"),
            # SMTP / Email
            ("ops_09_email_config", "/admin/system/email"),
            # RADIUS
            ("ops_10_radius_config", "/admin/system/config/radius"),
            # Network Config
            ("ops_11_cpe_config", "/admin/system/config/cpe"),
            ("ops_12_monitoring_config", "/admin/system/config/monitoring"),
            # Catalog
            ("ops_13_catalog_offers", "/admin/catalog"),
            ("ops_14_catalog_settings", "/admin/catalog/settings"),
            # Customers
            ("ops_15_customer_list", "/admin/customers"),
            # Subscriptions
            ("ops_16_subscriptions", "/admin/catalog/subscriptions"),
            # Billing
            ("ops_17_billing_overview", "/admin/billing"),
            ("ops_18_invoices", "/admin/billing/invoices"),
            # Network
            ("ops_19_nas_devices", "/admin/network/nas"),
            ("ops_20_olts", "/admin/network/olts"),
            ("ops_21_onts", "/admin/network/onts"),
            ("ops_22_tr069", "/admin/network/tr069"),
            # Provisioning
            ("ops_23_provisioning", "/admin/provisioning"),
            # Monitoring
            ("ops_24_monitoring", "/admin/network/monitoring"),
            ("ops_25_alarms", "/admin/network/alarms"),
            # Topology
            ("ops_26_topology", "/admin/network/topology"),
            # GIS
            ("ops_27_gis", "/admin/gis"),
            # Notifications
            ("ops_28_notification_templates", "/admin/notifications/templates"),
            ("ops_29_notification_queue", "/admin/notifications/queue"),
            # Reports
            ("ops_30_reports_hub", "/admin/reports/hub"),
            ("ops_31_reports_revenue", "/admin/reports/revenue"),
            ("ops_32_reports_bandwidth", "/admin/reports/bandwidth"),
            # System
            ("ops_33_users", "/admin/system/users"),
            ("ops_34_roles", "/admin/system/roles"),
            ("ops_35_secrets", "/admin/system/secrets"),
            ("ops_36_health", "/admin/system/health"),
            ("ops_37_scheduler", "/admin/system/scheduler"),
            ("ops_38_webhooks", "/admin/system/webhooks"),
            ("ops_39_api_keys", "/admin/system/api-keys"),
            # Integrations
            ("ops_40_integrations", "/admin/integrations/connectors"),
        ]

        for name, url in configs:
            try:
                capture(page, name, url)
            except Exception as e:
                print(f"  [{name}] FAILED: {e}")

        browser.close()
        print(f"\n==> Done! Screenshots in {OUT_DIR}/")
        print(f"    Total: {len(list(OUT_DIR.glob('*.png')))} files")


if __name__ == "__main__":
    main()
