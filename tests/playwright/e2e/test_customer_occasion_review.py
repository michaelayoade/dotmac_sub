"""Desktop/mobile review must not activate customer greetings or send mail."""

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.helpers.config import E2ESettings


@pytest.mark.parametrize("width", [1440, 390])
def test_customer_occasion_review_stays_disabled(
    admin_page: Page, settings: E2ESettings, width: int
) -> None:
    admin_page.set_viewport_size({"width": width, "height": 900})
    response = admin_page.goto(f"{settings.base_url}/admin/notifications/occasions")
    assert response is not None and response.ok
    expect(
        admin_page.get_by_role(
            "heading", name="Birthday and service anniversary emails"
        )
    ).to_be_visible()
    for occasion in ("birthday", "anniversary"):
        form = admin_page.locator(
            f'form[action="/admin/notifications/occasions/{occasion}"]'
        )
        expect(form.locator('input[name="enabled"]')).not_to_be_checked()
        expect(
            form.get_by_role("button", name=f"Save {occasion} settings")
        ).to_be_visible()
    expect(admin_page.get_by_text("Disabled", exact=True)).to_have_count(2)
    expect(
        admin_page.get_by_text("active and suspended", exact=False).first
    ).to_be_visible()
    assert admin_page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )

    # Save the reviewed content without opting in; this exercises the real
    # cookie-authenticated CSRF form boundary and revision handling.
    admin_page.get_by_role("button", name="Save birthday settings").click()
    expect(
        admin_page.get_by_role("status").filter(has_text="Settings saved")
    ).to_be_visible()
    for occasion in ("birthday", "anniversary"):
        form = admin_page.locator(
            f'form[action="/admin/notifications/occasions/{occasion}"]'
        )
        expect(form.locator('input[name="enabled"]')).not_to_be_checked()
