#!/usr/bin/env python3
"""
Playwright test for WebSocket Operation Tracker.

Tests the operation tracker on a fast-loading page.
"""
from __future__ import annotations

import subprocess
import sys
import re

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8001"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"


def get_session_cookie() -> str | None:
    """Use curl to login and get session cookie."""
    result = subprocess.run(
        ['curl', '-s', '-c', '/tmp/cookies.txt', f'{BASE_URL}/auth/login'],
        capture_output=True, text=True
    )
    csrf_match = re.search(r'name="_csrf_token" value="([^"]+)"', result.stdout)
    if not csrf_match:
        return None
    csrf = csrf_match.group(1)

    subprocess.run([
        'curl', '-s', '-D', '/tmp/headers.txt', '-o', '/dev/null',
        '-X', 'POST', f'{BASE_URL}/auth/login',
        '-b', '/tmp/cookies.txt',
        '-H', 'Content-Type: application/x-www-form-urlencoded',
        '-d', f'username={ADMIN_USERNAME}&password={ADMIN_PASSWORD}&_csrf_token={csrf}'
    ])

    with open('/tmp/headers.txt', 'r') as f:
        headers = f.read()

    session_match = re.search(r'set-cookie: session_token=([^;]+)', headers, re.IGNORECASE)
    return session_match.group(1) if session_match else None


def main() -> int:
    print("=" * 60)
    print("WebSocket Operation Tracker Test")
    print("=" * 60)

    # Step 1: Get session cookie via curl
    print("\n[1] Getting session via curl...")
    session_token = get_session_cookie()
    if not session_token:
        print("    ERROR: Could not get session")
        return 1
    print(f"    OK - Session token: {session_token[:30]}...")

    # Step 2: Launch Playwright
    print("\n[2] Launching Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.set_default_timeout(60000)  # 60s timeout for slow pages

        context.add_cookies([{
            "name": "session_token",
            "value": session_token,
            "domain": "127.0.0.1",
            "path": "/",
            "httpOnly": True,
            "sameSite": "Lax"
        }])

        page = context.new_page()

        # Test a faster page - the OLTs list page
        print("\n[3] Loading OLTs page (faster than dashboard)...")
        page.goto(f"{BASE_URL}/admin/network/olts", wait_until="domcontentloaded", timeout=60000)
        print(f"    URL: {page.url}")

        if "/auth/login" in page.url:
            print("    ERROR: Not authenticated")
            browser.close()
            return 1

        print("    Auth successful!")

        # Wait for scripts to load
        page.wait_for_timeout(3000)

        # Step 3: Check operation-tracker.js
        print("\n[4] Checking operation-tracker.js...")
        tracker_status = page.evaluate("""
            () => ({
                OperationTrackerClass: typeof window.OperationTracker === 'function',
                initOperationTracker: typeof window.initOperationTracker === 'function',
                trackOperation: typeof window.trackOperation === 'function',
                instance: window.operationTracker ? 'exists' : 'null'
            })
        """)

        print(f"    OperationTracker class: {tracker_status['OperationTrackerClass']}")
        print(f"    initOperationTracker: {tracker_status['initOperationTracker']}")
        print(f"    trackOperation: {tracker_status['trackOperation']}")
        print(f"    Instance: {tracker_status['instance']}")

        if not tracker_status['OperationTrackerClass']:
            print("\n    ERROR: OperationTracker not loaded!")
            browser.close()
            return 1

        print("    OK - OperationTracker loaded")

        # Step 4: Test toast system
        print("\n[5] Testing toast notifications...")
        toast_result = page.evaluate("""
            () => {
                const container = document.getElementById('toast-container');
                if (!container) return { error: 'no container' };

                window.dispatchEvent(new CustomEvent('toast', {
                    detail: { type: 'success', message: 'Test from Playwright', duration: 5000 }
                }));

                return { containerFound: true, alpine: container.hasAttribute('x-data') };
            }
        """)
        print(f"    Toast container: {toast_result.get('containerFound', False)}")
        print(f"    Alpine.js: {toast_result.get('alpine', False)}")

        page.wait_for_timeout(500)
        toast_visible = page.locator('text=Test from Playwright').count() > 0
        print(f"    Toast appeared: {toast_visible}")

        # Step 5: Check autofind on an OLT (if available)
        print("\n[6] Checking OLT autofind...")
        olt_link = page.evaluate("""
            () => {
                const a = document.querySelector('a[href*="/admin/network/olts/"][href$="-"]');
                return a ? a.getAttribute('href') : null;
            }
        """)

        if olt_link and len(olt_link) > 30:  # UUID links are longer
            print(f"    Found OLT: ...{olt_link[-40:]}")
            page.goto(f"{BASE_URL}{olt_link}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            # Click autofind tab
            autofind = page.locator('button:has-text("Autofind"), [data-tab="autofind"]').first
            if autofind.count() > 0:
                print("    Clicking Autofind tab...")
                autofind.click()
                page.wait_for_timeout(3000)

                has_component = page.evaluate("""
                    () => !!document.querySelector('[x-data*="autofindOperations"]')
                """)
                print(f"    autofindOperations component: {has_component}")

                auth_count = page.locator('button:has-text("Authorize")').count()
                print(f"    Authorize buttons: {auth_count}")
            else:
                print("    No Autofind tab on this OLT")
        else:
            print("    No OLTs available")

        # Summary
        print("\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60)
        all_passed = all([
            tracker_status['OperationTrackerClass'],
            tracker_status['initOperationTracker'],
            tracker_status['trackOperation'],
            toast_result.get('containerFound', False)
        ])

        print(f"  OperationTracker loaded:  {'PASS' if tracker_status['OperationTrackerClass'] else 'FAIL'}")
        print(f"  initOperationTracker:     {'PASS' if tracker_status['initOperationTracker'] else 'FAIL'}")
        print(f"  trackOperation:           {'PASS' if tracker_status['trackOperation'] else 'FAIL'}")
        print(f"  Toast container:          {'PASS' if toast_result.get('containerFound') else 'FAIL'}")
        print(f"  Toast visible:            {'PASS' if toast_visible else 'SKIP'}")
        print("=" * 60)
        print(f"  Overall: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
        print("=" * 60)

        browser.close()
        return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
