"""E2E: the plan-change editor renders its form contract.

Verifies the ui.form_contracts pilot end-to-end per the UI information/action
standard's Editor rules: the page shows the current state, names the
consequences of submitting before the primary action, and (when prerequisites
are unmet) explains them near the control. Impact preview is exercised via the
lazy proration quote on offer selection.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


class TestPlanChangeFormContract:
    def _open(self, customer_page: Page, settings, test_identities) -> str | None:
        subscription_id = test_identities["customer"].get("subscription_id")
        if not subscription_id:
            pytest.skip("No active customer subscription provisioned")
        customer_page.goto(
            f"{settings.base_url}/portal/services/{subscription_id}/change"
        )
        return subscription_id

    def test_shows_current_state(self, customer_page: Page, settings, test_identities):
        self._open(customer_page, settings, test_identities)
        current = customer_page.get_by_test_id("plan-change-current")
        expect(current).to_be_visible()
        expect(current).to_contain_text("Current Plan")

    def test_names_consequences_before_the_action(
        self, customer_page: Page, settings, test_identities
    ):
        self._open(customer_page, settings, test_identities)
        consequences = customer_page.get_by_test_id("plan-change-consequences")
        expect(consequences).to_be_visible()
        expect(consequences).to_contain_text("What happens when you switch")
        for key in ("proration", "reprovision", "cross_family"):
            expect(consequences.locator(f'[data-consequence="{key}"]')).to_be_visible()

    def test_prerequisites_shown_only_when_unmet(
        self, customer_page: Page, settings, test_identities
    ):
        self._open(customer_page, settings, test_identities)
        # The provisioned e2e customer has an active, arrears-free subscription
        # with available offers — all prerequisites met, so the block is absent
        # (prerequisite disclosure appears only when something blocks submit).
        expect(customer_page.get_by_test_id("plan-change-prerequisites")).to_have_count(
            0
        )

    def test_offer_selection_fetches_impact_quote(
        self, customer_page: Page, settings, test_identities
    ):
        subscription_id = self._open(customer_page, settings, test_identities)
        radios = customer_page.locator('input[name="offer_id"]')
        if radios.count() == 0:
            pytest.skip("No alternative offers available to select")
        with customer_page.expect_response(
            lambda r: f"/portal/services/{subscription_id}/change/quote" in r.url
        ) as quote_response:
            # The radio is sr-only (the styled label is the visual control), so
            # bypass actionability and let the change event fire the quote fetch.
            radios.first.check(force=True)
        assert quote_response.value.status in (200, 404)
