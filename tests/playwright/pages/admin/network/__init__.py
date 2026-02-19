"""Network page objects."""

from tests.playwright.pages.admin.network.fiber_map_page import FiberMapPage
from tests.playwright.pages.admin.network.ip_management_page import IPManagementPage
from tests.playwright.pages.admin.network.olts_page import OLTsPage
from tests.playwright.pages.admin.network.onts_page import ONTsPage
from tests.playwright.pages.admin.network.vlans_page import VLANsPage

__all__ = [
    "OLTsPage",
    "ONTsPage",
    "VLANsPage",
    "IPManagementPage",
    "FiberMapPage",
]
