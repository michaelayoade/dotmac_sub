"""Support-ticket admin interaction tests."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def test_column_picker_closes_from_trigger_and_outside_click(
    admin_page: Page, settings
) -> None:
    """The column picker must not trap the operator in its open state."""

    admin_page.goto(f"{settings.base_url}/admin/support/tickets")

    trigger = admin_page.get_by_role("button", name="Columns")
    panel = admin_page.locator("#ticket-column-options")

    trigger.click()
    expect(panel).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")

    trigger.click()
    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")

    trigger.click()
    expect(panel).to_be_visible()

    form = admin_page.locator("#ticket-filter-form")
    form.evaluate("element => element.dataset.e2eBeforeApply = 'true'")
    admin_page.get_by_role("button", name="Apply ticket filters").click()
    expect(form).to_have_attribute(
        "data-e2e-before-apply",
        "true",
    )
    expect(panel).to_be_hidden()

    trigger.click()
    expect(panel).to_be_visible()

    admin_page.get_by_role("heading", name="Support Tickets").click()
    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")

    trigger.evaluate(
        "element => { element.dataset.filterRefreshIdentity = 'preserved'; }"
    )
    admin_page.locator("#ticket-status-filter").select_option("not_closed")
    expect(admin_page).to_have_url(re.compile(r".*status=not_closed.*"))

    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_have_attribute("data-filter-refresh-identity", "preserved")


def test_filter_feedback_reports_loading_and_keeps_results_on_failure(
    admin_page: Page, settings
) -> None:
    admin_page.goto(f"{settings.base_url}/admin/support/tickets")
    held_routes = []
    admin_page.route(
        "**/admin/support/tickets?**", lambda route: held_routes.append(route)
    )

    admin_page.locator("#ticket-status-filter").select_option("not_closed")
    expect(admin_page.get_by_text("Updating tickets…", exact=True)).to_be_visible()
    assert held_routes

    held_routes.pop().abort("failed")

    error = admin_page.get_by_role("alert")
    expect(error).to_contain_text(
        "Couldn’t update tickets. Your current results are still shown."
    )
    expect(error.get_by_role("button", name="Retry")).to_be_visible()
    expect(admin_page.locator("#tickets-table")).to_be_visible()


def test_applied_filter_is_restored_after_returning_from_ticket_detail(
    admin_page: Page, settings
) -> None:
    admin_page.goto(f"{settings.base_url}/admin/support/tickets")
    admin_page.get_by_role("button", name="Clear ticket filters").click()
    admin_page.wait_for_url("**/admin/support/tickets")

    admin_page.locator("#ticket-status-filter").select_option("not_closed")
    expect(admin_page).to_have_url(re.compile(r".*status=not_closed.*"))
    filtered_url = admin_page.url

    ticket_link = admin_page.locator(
        "#tickets-table tbody a[href^='/admin/support/tickets/']"
    ).first
    expect(ticket_link).to_be_visible()
    ticket_link.click()
    admin_page.wait_for_url("**/admin/support/tickets/**")

    admin_page.locator("a[href='/admin/support/tickets']").first.click()
    expect(admin_page).to_have_url(
        re.compile(r".*/admin/support/tickets\?.*status=not_closed.*")
    )
    expect(admin_page.locator("#ticket-status-filter")).to_have_value("not_closed")


def test_comment_submit_lock_allows_one_in_flight_request_and_resets_on_error(
    admin_page: Page, settings
) -> None:
    admin_page.goto(f"{settings.base_url}/admin/support/tickets")
    admin_page.locator(
        "#tickets-table tbody a[href^='/admin/support/tickets/']"
    ).first.click()
    admin_page.wait_for_url("**/admin/support/tickets/**")

    comment_form = admin_page.locator("form[action$='/comment']")
    comment_form.locator("textarea[name='body']").fill("Submit lock proof")
    error_page = admin_page.content()
    held_routes = []
    admin_page.route(
        "**/admin/support/tickets/*/comment",
        lambda route: held_routes.append(route),
    )

    submit = comment_form.locator("button[type='submit']")
    # Schedule submission on the next browser task so this evaluation returns
    # before the deliberately held navigation begins.
    comment_form.evaluate("form => setTimeout(() => form.requestSubmit(), 0)")
    expect(submit).to_be_disabled()
    expect(submit).to_have_attribute("aria-busy", "true")
    comment_form.evaluate("form => form.requestSubmit()")

    assert len(held_routes) == 1
    held_routes[0].fulfill(
        status=422,
        content_type="text/html",
        body=error_page,
    )
    admin_page.wait_for_load_state("domcontentloaded")

    expect(
        admin_page.locator("form[action$='/comment'] button[type='submit']")
    ).to_be_enabled()
