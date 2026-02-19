"""Catalog services package.

This package provides catalog-related services including offers, subscriptions,
add-ons, RADIUS profiles, NAS devices, and policy management.

All existing import patterns are preserved for backward compatibility:
    from app.services import catalog as catalog_service
    catalog_service.offers.create(db, payload)

    from app.services.catalog import Offers, offers
"""

from app.services.catalog.add_ons import AddOnPrices, AddOns
from app.services.catalog.credentials import AccessCredentials
from app.services.catalog.nas import NasDevices
from app.services.catalog.offer_addons import OfferAddOns
from app.services.catalog.offers import (
    OfferPrices,
    Offers,
    OfferVersionPrices,
    OfferVersions,
)
from app.services.catalog.policies import PolicyDunningSteps, PolicySets
from app.services.catalog.profiles import RegionZones, SlaProfiles, UsageAllowances
from app.services.catalog.radius import (
    OfferRadiusProfiles,
    RadiusAttributes,
    RadiusProfiles,
)
from app.services.catalog.subscriptions import SubscriptionAddOns, Subscriptions
from app.services.catalog.validation import OfferValidation

# Singleton instances for service access
offers = Offers()
subscriptions = Subscriptions()
subscription_add_ons = SubscriptionAddOns()
offer_versions = OfferVersions()
offer_version_prices = OfferVersionPrices()
region_zones = RegionZones()
usage_allowances = UsageAllowances()
sla_profiles = SlaProfiles()
policy_sets = PolicySets()
policy_dunning_steps = PolicyDunningSteps()
add_ons = AddOns()
offer_addons = OfferAddOns()
offer_prices = OfferPrices()
add_on_prices = AddOnPrices()
nas_devices = NasDevices()
radius_profiles = RadiusProfiles()
radius_attributes = RadiusAttributes()
offer_radius_profiles = OfferRadiusProfiles()
access_credentials = AccessCredentials()
offer_validation = OfferValidation()

__all__ = [
    # Classes
    "Offers",
    "OfferPrices",
    "OfferVersions",
    "OfferVersionPrices",
    "Subscriptions",
    "SubscriptionAddOns",
    "AddOns",
    "AddOnPrices",
    "OfferAddOns",
    "RegionZones",
    "UsageAllowances",
    "SlaProfiles",
    "PolicySets",
    "PolicyDunningSteps",
    "RadiusProfiles",
    "RadiusAttributes",
    "OfferRadiusProfiles",
    "NasDevices",
    "AccessCredentials",
    "OfferValidation",
    # Singleton instances
    "offers",
    "subscriptions",
    "subscription_add_ons",
    "offer_versions",
    "offer_version_prices",
    "region_zones",
    "usage_allowances",
    "sla_profiles",
    "policy_sets",
    "policy_dunning_steps",
    "add_ons",
    "offer_addons",
    "offer_prices",
    "add_on_prices",
    "nas_devices",
    "radius_profiles",
    "radius_attributes",
    "offer_radius_profiles",
    "access_credentials",
    "offer_validation",
]
