from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.set_default_timeout(15000)

        print("open-login")
        page.goto("http://127.0.0.1:8001/auth/login?next=/admin/dashboard", wait_until="domcontentloaded", timeout=30000)
        page.screenshot(path="screenshots/dashboard-login-review.png")
        print(f"login-url: {page.url}")
        print(f"title: {page.title()}")
        body_text = page.locator("body").inner_text()
        print("body-preview:", body_text[:2000])
        inputs = page.locator("input")
        print(f"input-count: {inputs.count()}")
        for i in range(inputs.count()):
            locator = inputs.nth(i)
            print(
                "input:",
                i,
                locator.get_attribute("name"),
                locator.get_attribute("type"),
                locator.get_attribute("placeholder"),
            )
        buttons = page.locator("button")
        print(f"button-count: {buttons.count()}")
        for i in range(buttons.count()):
            print("button:", i, buttons.nth(i).inner_text())

        page.locator('input[name="email"], input[name="username"], input[name="identifier"], input[type="email"], input[type="text"]').first.fill("admin")
        page.locator('input[name="password"], input[type="password"]').first.fill("admin123")
        print("submit-login")
        page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Login")').first.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        print(f"post-login-url: {page.url}")

        print("open-dashboard")
        page.goto("http://127.0.0.1:8001/admin/dashboard", wait_until="domcontentloaded", timeout=30000)
        page.screenshot(path="screenshots/dashboard-live-review.png", full_page=True)
        print(f"title: {page.title()}")
        print(f"url: {page.url}")

        print("open-monitoring")
        page.goto("http://127.0.0.1:8001/admin/network/monitoring", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        page.screenshot(path="screenshots/network-monitoring-live-review.png", full_page=True)
        print(f"monitoring-title: {page.title()}")
        print(f"monitoring-url: {page.url}")

        print("open-subscribers")
        page.goto("http://127.0.0.1:8001/admin/subscribers", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        page.screenshot(path="screenshots/subscribers-list-live-review.png", full_page=True)
        print(f"subscribers-title: {page.title()}")
        print(f"subscribers-url: {page.url}")

        subscriber_href = page.evaluate(
            """
            () => {
              const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'));
              return links.find(href =>
                href &&
                href.startsWith('/admin/subscribers/') &&
                href !== '/admin/subscribers/' &&
                href !== '/admin/subscribers/new' &&
                !href.includes('/edit') &&
                !href.includes('/organization/')
              ) || null;
            }
            """
        )
        print(f"subscriber-href: {subscriber_href}")
        if subscriber_href:
            print("open-subscriber")
            page.goto(f"http://127.0.0.1:8001{subscriber_href}", wait_until="domcontentloaded", timeout=30000)
            page.screenshot(path="screenshots/subscriber-detail-live-review.png", full_page=True)
            print(f"subscriber-title: {page.title()}")
            print(f"subscriber-url: {page.url}")

        browser.close()


if __name__ == "__main__":
    main()
