"""Customer portal services page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerServicesPage(BasePage):
    """Page object for the customer services page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/customer/services") -> None:
        """Navigate to the services page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the services page is loaded."""
        expect(self.page.get_by_role("heading", name="Service", exact=True)).to_be_visible()

    def expect_active_services_visible(self) -> None:
        """Assert active services section is visible."""
        expect(self.page.get_by_text("Active", exact=False).first).to_be_visible()

    def expect_service_details_visible(self) -> None:
        """Assert service details are visible."""
        expect(self.page.locator("[data-testid='service-details']").or_(
            self.page.get_by_text("Plan", exact=False).or_(
                self.page.get_by_text("Speed", exact=False)
            )
        ).first).to_be_visible()

    def get_service_count(self) -> int:
        """Get count of services displayed."""
        services = self.page.locator("[data-testid='service-item']").or_(
            self.page.locator(".service-card")
        )
        return services.count()

    def click_service_details(self, service_name: str) -> None:
        """Click to view service details."""
        self.page.get_by_text(service_name).click()

    def expect_service_in_list(self, service_name: str) -> None:
        """Assert a service is visible in the list."""
        expect(self.page.get_by_text(service_name)).to_be_visible()

    def request_upgrade(self) -> None:
        """Request a service upgrade."""
        self.page.get_by_role("button", name="Upgrade").or_(
            self.page.get_by_role("link", name="Upgrade")
        ).first.click()

    def request_downgrade(self) -> None:
        """Request a service downgrade."""
        self.page.get_by_role("button", name="Downgrade").or_(
            self.page.get_by_role("link", name="Downgrade")
        ).first.click()

    def view_usage(self) -> None:
        """View usage for current service."""
        self.page.get_by_role("link", name="Usage").first.click()

    def get_current_plan_name(self) -> str:
        """Get current plan name."""
        plan_element = self.page.locator("[data-testid='current-plan']").or_(
            self.page.get_by_text("Plan", exact=False)
        ).first
        return plan_element.text_content() or ""

    def get_service_status(self) -> str:
        """Get service status."""
        status_element = self.page.locator("[data-testid='service-status']").or_(
            self.page.get_by_text("Active").or_(
                self.page.get_by_text("Suspended")
            )
        ).first
        return status_element.text_content() or ""
