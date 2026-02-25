"""Playwright script to review implemented Sprint 6 UI features."""

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8001"
SCREENSHOTS_DIR = "/tmp/ui_review"


def main():
    import os
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        # 1. Login
        print("Logging in...")
        page.goto(f"{BASE}/auth/login")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{SCREENSHOTS_DIR}/01_login.png")

        page.fill('input[name="username"]', "admin")
        page.fill('input[name="password"]', "admin123")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{SCREENSHOTS_DIR}/02_after_login.png")
        print(f"  Current URL: {page.url}")

        # 2. VLANs list — check Purpose column + DHCP snooping
        print("\nReviewing VLANs list page...")
        page.goto(f"{BASE}/admin/network/vlans")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{SCREENSHOTS_DIR}/03_vlans_list.png", full_page=True)
        print(f"  URL: {page.url}")

        # Check for Purpose column header
        purpose_header = page.query_selector("th:has-text('Purpose')")
        print(f"  Purpose column header: {'FOUND' if purpose_header else 'MISSING'}")

        # Check for DHCP snooping indicators
        dhcp_badges = page.query_selector_all("text=DHCP")
        print(f"  DHCP snooping indicators: {len(dhcp_badges)} found")

        # 3. VLAN create form — check Purpose dropdown + DHCP checkbox
        print("\nReviewing VLAN create form...")
        page.goto(f"{BASE}/admin/network/vlans/new")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{SCREENSHOTS_DIR}/04_vlan_form.png", full_page=True)
        print(f"  URL: {page.url}")

        purpose_select = page.query_selector("select[name='purpose']")
        print(f"  Purpose dropdown: {'FOUND' if purpose_select else 'MISSING'}")

        dhcp_checkbox = page.query_selector("input[name='dhcp_snooping']")
        print(f"  DHCP Snooping checkbox: {'FOUND' if dhcp_checkbox else 'MISSING'}")

        # List purpose options
        if purpose_select:
            options = purpose_select.query_selector_all("option")
            print(f"  Purpose options: {[o.text_content().strip() for o in options]}")

        # 4. VLAN detail — check for first VLAN
        print("\nReviewing VLAN detail page...")
        page.goto(f"{BASE}/admin/network/vlans")
        page.wait_for_load_state("networkidle")
        first_link = page.query_selector("table tbody tr a")
        if first_link:
            href = first_link.get_attribute("href")
            print(f"  Navigating to first VLAN: {href}")
            page.goto(f"{BASE}{href}")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{SCREENSHOTS_DIR}/05_vlan_detail.png", full_page=True)

            purpose_badge = page.query_selector("text=Purpose")
            print(f"  Purpose label: {'FOUND' if purpose_badge else 'MISSING'}")

            dhcp_label = page.query_selector("text=DHCP Snooping")
            print(f"  DHCP Snooping label: {'FOUND' if dhcp_label else 'MISSING'}")
        else:
            print("  No VLANs found in table")

        # 5. Network monitoring dashboard — ONU trend + activity feed
        print("\nReviewing Network Monitoring dashboard...")
        page.goto(f"{BASE}/admin/network/monitoring")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)  # Let charts render
        page.screenshot(path=f"{SCREENSHOTS_DIR}/06_monitoring_top.png")

        # Scroll down to see charts and activity feed
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SCREENSHOTS_DIR}/07_monitoring_mid.png")

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SCREENSHOTS_DIR}/08_monitoring_bottom.png", full_page=True)

        # Check for ONU auth trend chart
        auth_trend = page.query_selector("canvas#onuAuthTrendChart")
        print(f"  ONU Auth Trend chart canvas: {'FOUND' if auth_trend else 'MISSING'}")

        # Check for activity feed
        activity_heading = page.query_selector("text=Recent Network Events")
        print(f"  Activity feed heading: {'FOUND' if activity_heading else 'MISSING'}")

        # Check for ONU status trend chart
        onu_trend = page.query_selector("canvas#onuStatusTrendChart")
        print(f"  ONU Status Trend chart canvas: {'FOUND' if onu_trend else 'MISSING'}")

        # 6. OLT detail page — Config Backup History
        print("\nReviewing OLT detail page...")
        page.goto(f"{BASE}/admin/network/olts")
        page.wait_for_load_state("networkidle")
        first_olt = page.query_selector("table tbody tr a")
        if first_olt:
            href = first_olt.get_attribute("href")
            print(f"  Navigating to first OLT: {href}")
            page.goto(f"{BASE}{href}")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{SCREENSHOTS_DIR}/09_olt_detail_top.png")

            # Click Activity tab if present
            activity_tab = page.query_selector("[data-tab='activity'], button:has-text('Activity')")
            if activity_tab:
                activity_tab.click()
                page.wait_for_timeout(500)
                page.screenshot(path=f"{SCREENSHOTS_DIR}/10_olt_activity_tab.png", full_page=True)
                print(f"  Activity tab: FOUND and clicked")

                backup_heading = page.query_selector("text=Config Backup History")
                print(f"  Config Backup History: {'FOUND' if backup_heading else 'MISSING'}")
            else:
                print("  Activity tab: NOT FOUND")
                # Try scrolling to find backup section
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(300)
                page.screenshot(path=f"{SCREENSHOTS_DIR}/10_olt_detail_bottom.png", full_page=True)
        else:
            print("  No OLTs found")

        # 7. ONT detail page — TR-069 tab with WiFi/LAN controls
        print("\nReviewing ONT detail page (TR-069 tab)...")
        page.goto(f"{BASE}/admin/network/onts")
        page.wait_for_load_state("networkidle")
        first_ont = page.query_selector("table tbody tr a")
        if first_ont:
            href = first_ont.get_attribute("href")
            print(f"  Navigating to first ONT: {href}")
            page.goto(f"{BASE}{href}")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{SCREENSHOTS_DIR}/11_ont_detail.png")

            # Click TR-069 tab
            tr069_tab = page.query_selector("[data-tab='tr069'], button:has-text('TR-069'), a:has-text('TR-069')")
            if tr069_tab:
                tr069_tab.click()
                page.wait_for_timeout(1000)
                page.screenshot(path=f"{SCREENSHOTS_DIR}/12_ont_tr069_tab.png", full_page=True)
                print(f"  TR-069 tab: FOUND and clicked")

                ssid_btn = page.query_selector("button:has-text('Change SSID')")
                print(f"  Change SSID button: {'FOUND' if ssid_btn else 'MISSING'}")

                pwd_btn = page.query_selector("button:has-text('Change Password')")
                print(f"  Change Password button: {'FOUND' if pwd_btn else 'MISSING'}")
            else:
                print("  TR-069 tab: NOT FOUND")
        else:
            print("  No ONTs found")

        browser.close()
        print(f"\nScreenshots saved to {SCREENSHOTS_DIR}/")
        print("Review complete!")


if __name__ == "__main__":
    main()
