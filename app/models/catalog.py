import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceType(enum.Enum):
    residential = "residential"
    business = "business"


class AccessType(enum.Enum):
    fiber = "fiber"
    fixed_wireless = "fixed_wireless"
    dsl = "dsl"
    cable = "cable"


class PriceBasis(enum.Enum):
    flat = "flat"
    usage = "usage"
    tiered = "tiered"
    hybrid = "hybrid"


class BillingCycle(enum.Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    annual = "annual"


class ContractTerm(enum.Enum):
    month_to_month = "month_to_month"
    twelve_month = "twelve_month"
    twentyfour_month = "twentyfour_month"


class PriceType(enum.Enum):
    recurring = "recurring"
    one_time = "one_time"
    usage = "usage"


class PriceUnit(enum.Enum):
    day = "day"
    week = "week"
    month = "month"
    year = "year"
    gb = "gb"
    tb = "tb"
    item = "item"


class GuaranteedSpeedType(enum.Enum):
    none = "none"
    relative = "relative"
    fixed = "fixed"


class ProrationPolicy(enum.Enum):
    immediate = "immediate"
    next_cycle = "next_cycle"
    none = "none"


class SuspensionAction(enum.Enum):
    none = "none"
    throttle = "throttle"
    suspend = "suspend"
    reject = "reject"


class RefundPolicy(enum.Enum):
    none = "none"
    prorated = "prorated"
    full_within_days = "full_within_days"


class DunningAction(enum.Enum):
    notify = "notify"
    throttle = "throttle"
    suspend = "suspend"
    reject = "reject"


class AddOnType(enum.Enum):
    static_ip = "static_ip"
    router_rental = "router_rental"
    install_fee = "install_fee"
    premium_support = "premium_support"
    extra_ip = "extra_ip"
    managed_wifi = "managed_wifi"
    custom = "custom"


class OfferStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    archived = "archived"


class BillingMode(enum.Enum):
    prepaid = "prepaid"
    postpaid = "postpaid"


class SubscriptionStatus(enum.Enum):
    pending = "pending"
    active = "active"
    suspended = "suspended"
    canceled = "canceled"
    expired = "expired"


class NasVendor(enum.Enum):
    """Supported NAS device vendors."""
    mikrotik = "mikrotik"
    huawei = "huawei"
    ubiquiti = "ubiquiti"
    cisco = "cisco"
    juniper = "juniper"
    cambium = "cambium"
    nokia = "nokia"
    zte = "zte"
    other = "other"


class ConnectionType(enum.Enum):
    """Network connection/authentication protocol type."""
    pppoe = "pppoe"          # Point-to-Point Protocol over Ethernet
    dhcp = "dhcp"            # Dynamic Host Configuration Protocol (no auth)
    ipoe = "ipoe"            # IP over Ethernet (DHCP + RADIUS Option 82)
    static = "static"        # Static IP assignment
    hotspot = "hotspot"      # Web portal login (MikroTik specific)


class NasDeviceStatus(enum.Enum):
    """NAS device operational status."""
    active = "active"
    maintenance = "maintenance"
    offline = "offline"
    decommissioned = "decommissioned"


class HealthStatus(enum.Enum):
    unknown = "unknown"
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class ProvisioningLogStatus(enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    timeout = "timeout"


class ExecutionMethod(enum.Enum):
    ssh = "ssh"
    api = "api"
    radius_coa = "radius_coa"


class DiscountType(enum.Enum):
    percentage = "percentage"
    # Legacy value kept for backward compatibility with older imported data.
    percent = "percent"
    fixed = "fixed"


class ConfigBackupMethod(enum.Enum):
    """Methods for backing up device configuration."""
    ssh = "ssh"
    api = "api"
    tftp = "tftp"
    ftp = "ftp"
    snmp = "snmp"


class ProvisioningAction(enum.Enum):
    """Types of provisioning actions."""
    create_user = "create_user"
    delete_user = "delete_user"
    suspend_user = "suspend_user"
    unsuspend_user = "unsuspend_user"
    change_speed = "change_speed"
    change_ip = "change_ip"
    reset_session = "reset_session"
    get_user_info = "get_user_info"
    backup_config = "backup_config"
    restore_config = "restore_config"


class RegionZone(Base):
    __tablename__ = "region_zones"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offers = relationship("CatalogOffer", back_populates="region_zone")


class PolicySet(Base):
    __tablename__ = "policy_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    proration_policy: Mapped[ProrationPolicy] = mapped_column(
        Enum(ProrationPolicy), default=ProrationPolicy.immediate
    )
    downgrade_policy: Mapped[ProrationPolicy] = mapped_column(
        Enum(ProrationPolicy), default=ProrationPolicy.next_cycle
    )
    trial_days: Mapped[int | None] = mapped_column(Integer)
    trial_card_required: Mapped[bool] = mapped_column(Boolean, default=False)
    grace_days: Mapped[int | None] = mapped_column(Integer)
    suspension_action: Mapped[SuspensionAction] = mapped_column(
        Enum(SuspensionAction), default=SuspensionAction.suspend
    )
    refund_policy: Mapped[RefundPolicy] = mapped_column(
        Enum(RefundPolicy), default=RefundPolicy.none
    )
    refund_window_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    dunning_steps = relationship("PolicyDunningStep", back_populates="policy_set")
    offers = relationship("CatalogOffer", back_populates="policy_set")


class PolicyDunningStep(Base):
    __tablename__ = "policy_dunning_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id"), nullable=False
    )
    day_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[DunningAction] = mapped_column(Enum(DunningAction), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))

    policy_set = relationship("PolicySet", back_populates="dunning_steps")


class UsageAllowance(Base):
    __tablename__ = "usage_allowances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    included_gb: Mapped[int | None] = mapped_column(Integer)
    overage_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    overage_cap_gb: Mapped[int | None] = mapped_column(Integer)
    throttle_rate_mbps: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offers = relationship("CatalogOffer", back_populates="usage_allowance")


class SlaProfile(Base):
    __tablename__ = "sla_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    uptime_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    response_time_hours: Mapped[int | None] = mapped_column(Integer)
    resolution_time_hours: Mapped[int | None] = mapped_column(Integer)
    credit_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offers = relationship("CatalogOffer", back_populates="sla_profile")


class AddOn(Base):
    __tablename__ = "add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    addon_type: Mapped[AddOnType] = mapped_column(
        Enum(AddOnType), default=AddOnType.custom
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offer_links = relationship("OfferAddOn", back_populates="add_on")
    prices = relationship("AddOnPrice", back_populates="add_on")


class CatalogOffer(Base):
    __tablename__ = "catalog_offers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType), nullable=False)
    access_type: Mapped[AccessType] = mapped_column(Enum(AccessType), nullable=False)
    price_basis: Mapped[PriceBasis] = mapped_column(Enum(PriceBasis), nullable=False)
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        Enum(BillingCycle), default=BillingCycle.monthly
    )
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode), default=BillingMode.prepaid
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    region_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    usage_allowance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_allowances.id")
    )
    sla_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_profiles.id")
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    splynx_tariff_id: Mapped[int | None] = mapped_column(Integer)
    splynx_service_name: Mapped[str | None] = mapped_column(String(160))
    splynx_tax_id: Mapped[int | None] = mapped_column(Integer)
    with_vat: Mapped[bool] = mapped_column(Boolean, default=False)
    vat_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    speed_download_mbps: Mapped[int | None] = mapped_column(Integer)
    speed_upload_mbps: Mapped[int | None] = mapped_column(Integer)
    guaranteed_speed_limit_at: Mapped[int | None] = mapped_column(Integer)
    guaranteed_speed: Mapped[GuaranteedSpeedType] = mapped_column(
        Enum(GuaranteedSpeedType), default=GuaranteedSpeedType.none
    )
    aggregation: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[str | None] = mapped_column(String(40))
    available_for_services: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_customer_portal: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus), default=OfferStatus.active
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    region_zone = relationship("RegionZone", back_populates="offers")
    usage_allowance = relationship("UsageAllowance", back_populates="offers")
    sla_profile = relationship("SlaProfile", back_populates="offers")
    policy_set = relationship("PolicySet", back_populates="offers")
    prices = relationship("OfferPrice", back_populates="offer")
    add_on_links = relationship("OfferAddOn", back_populates="offer")
    radius_profiles = relationship("OfferRadiusProfile", back_populates="offer")
    subscriptions = relationship("Subscription", back_populates="offer")
    versions = relationship("OfferVersion", back_populates="offer")


class OfferVersion(Base):
    __tablename__ = "offer_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType), nullable=False)
    access_type: Mapped[AccessType] = mapped_column(Enum(AccessType), nullable=False)
    price_basis: Mapped[PriceBasis] = mapped_column(Enum(PriceBasis), nullable=False)
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        Enum(BillingCycle), default=BillingCycle.monthly
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    region_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    usage_allowance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_allowances.id")
    )
    sla_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_profiles.id")
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus), default=OfferStatus.active
    )
    description: Mapped[str | None] = mapped_column(Text)
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offer = relationship("CatalogOffer", back_populates="versions")
    region_zone = relationship("RegionZone")
    usage_allowance = relationship("UsageAllowance")
    sla_profile = relationship("SlaProfile")
    policy_set = relationship("PolicySet")
    prices = relationship("OfferVersionPrice", back_populates="offer_version")
    subscriptions = relationship("Subscription", back_populates="offer_version")


class OfferVersionPrice(Base):
    __tablename__ = "offer_version_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_versions.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offer_version = relationship("OfferVersion", back_populates="prices")


class OfferAddOn(Base):
    __tablename__ = "offer_add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    min_quantity: Mapped[int | None] = mapped_column(Integer)
    max_quantity: Mapped[int | None] = mapped_column(Integer)

    offer = relationship("CatalogOffer", back_populates="add_on_links")
    add_on = relationship("AddOn", back_populates="offer_links")


class OfferPrice(Base):
    __tablename__ = "offer_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    offer = relationship("CatalogOffer", back_populates="prices")


class AddOnPrice(Base):
    __tablename__ = "add_on_prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    price_type: Mapped[PriceType] = mapped_column(
        Enum(PriceType), default=PriceType.recurring
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    billing_cycle: Mapped[BillingCycle | None] = mapped_column(Enum(BillingCycle))
    unit: Mapped[PriceUnit | None] = mapped_column(Enum(PriceUnit))
    description: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    add_on = relationship("AddOn", back_populates="prices")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    offer_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_versions.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )

    # Provisioning - which NAS handles this subscription
    provisioning_nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    # Override RADIUS profile (instead of offer's default)
    radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id")
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.pending
    )
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(BillingMode), default=BillingMode.prepaid
    )
    contract_term: Mapped[ContractTerm] = mapped_column(
        Enum(ContractTerm), default=ContractTerm.month_to_month
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_billing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    splynx_service_id: Mapped[int | None] = mapped_column(Integer)
    router_id: Mapped[int | None] = mapped_column(Integer)
    service_description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(40))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount: Mapped[bool] = mapped_column(Boolean, default=False)
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_type: Mapped[DiscountType | None] = mapped_column(
        Enum(DiscountType, values_callable=lambda x: [e.value for e in x]),
    )
    service_status_raw: Mapped[str | None] = mapped_column(String(40))
    login: Mapped[str | None] = mapped_column(String(120))
    ipv4_address: Mapped[str | None] = mapped_column(String(64))
    ipv6_address: Mapped[str | None] = mapped_column(String(128))
    mac_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="subscriptions")
    offer = relationship("CatalogOffer", back_populates="subscriptions")
    offer_version = relationship("OfferVersion", back_populates="subscriptions")
    service_address = relationship("Address")
    provisioning_nas_device = relationship("NasDevice", back_populates="subscriptions")
    radius_profile = relationship("RadiusProfile", back_populates="subscriptions")
    add_ons = relationship("SubscriptionAddOn", back_populates="subscription")
    service_orders = relationship("ServiceOrder", back_populates="subscription")
    cpe_devices = relationship("CPEDevice", back_populates="subscription")
    ip_assignments = relationship("IPAssignment", back_populates="subscription")
    ont_assignments = relationship("OntAssignment", back_populates="subscription")
    lifecycle_events = relationship(
        "SubscriptionLifecycleEvent", back_populates="subscription"
    )
    bandwidth_samples = relationship("BandwidthSample", back_populates="subscription")
    usage_charges = relationship("UsageCharge", back_populates="subscription")
    quota_buckets = relationship("QuotaBucket", back_populates="subscription")


class SubscriptionAddOn(Base):
    __tablename__ = "subscription_add_ons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    add_on_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("add_ons.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscription = relationship("Subscription", back_populates="add_ons")
    add_on = relationship("AddOn")


class NasDevice(Base):
    """
    Network Access Server (NAS) device for subscriber authentication.

    NAS devices are routers, OLTs, or access points that:
    - Authenticate subscribers via RADIUS
    - Enforce bandwidth/QoS profiles
    - Can be provisioned with subscriber credentials
    """
    __tablename__ = "nas_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Basic Information
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]),
        default=NasVendor.other
    )
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)

    # Location
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    rack_position: Mapped[str | None] = mapped_column(String(40))

    # Network Configuration (renamed from ip_address for clarity)
    ip_address: Mapped[str | None] = mapped_column(String(64))  # Keep for backward compat
    management_ip: Mapped[str | None] = mapped_column(String(64))
    management_port: Mapped[int | None] = mapped_column(Integer, default=22)
    nas_ip: Mapped[str | None] = mapped_column(String(64))  # IP used in RADIUS requests

    # RADIUS Configuration
    shared_secret: Mapped[str | None] = mapped_column(String(255))  # Keep existing
    coa_port: Mapped[int | None] = mapped_column(Integer, default=3799)

    # Management Credentials
    ssh_username: Mapped[str | None] = mapped_column(String(120))
    ssh_password: Mapped[str | None] = mapped_column(String(255))
    ssh_key: Mapped[str | None] = mapped_column(Text)
    ssh_verify_host_key: Mapped[bool] = mapped_column(Boolean, default=False)
    api_username: Mapped[str | None] = mapped_column(String(120))
    api_password: Mapped[str | None] = mapped_column(String(255))
    api_token: Mapped[str | None] = mapped_column(Text)
    api_url: Mapped[str | None] = mapped_column(String(500))
    api_verify_tls: Mapped[bool] = mapped_column(Boolean, default=False)

    # SNMP Configuration
    snmp_community: Mapped[str | None] = mapped_column(String(120))
    snmp_version: Mapped[str | None] = mapped_column(String(10), default="2c")
    snmp_port: Mapped[int | None] = mapped_column(Integer, default=161)

    # Connection Types (JSON array of ConnectionType values)
    supported_connection_types: Mapped[list | None] = mapped_column(
        JSONB, default=lambda: ["pppoe"]
    )
    default_connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x])
    )

    # Configuration Backup Settings
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_method: Mapped[ConfigBackupMethod | None] = mapped_column(
        Enum(ConfigBackupMethod, values_callable=lambda x: [e.value for e in x])
    )
    backup_schedule: Mapped[str | None] = mapped_column(String(60))  # cron expression
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status
    status: Mapped[NasDeviceStatus] = mapped_column(
        Enum(NasDeviceStatus, values_callable=lambda x: [e.value for e in x]),
        default=NasDeviceStatus.active
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Capacity tracking
    max_concurrent_subscribers: Mapped[int | None] = mapped_column(Integer)
    current_subscriber_count: Mapped[int] = mapped_column(Integer, default=0)

    # Health tracking
    health_status: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, values_callable=lambda x: [e.value for e in x]),
        default=HealthStatus.unknown,
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Metadata
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSONB)

    # Link to network_device for monitoring integration
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # Relationships
    pop_site = relationship("PopSite", back_populates="nas_devices")
    network_device = relationship("NetworkDevice", back_populates="nas_device")
    radius_clients = relationship("RadiusClient", back_populates="nas_device")
    config_backups = relationship("NasConfigBackup", back_populates="nas_device")
    provisioning_logs = relationship("ProvisioningLog", back_populates="nas_device")
    subscriptions = relationship("Subscription", back_populates="provisioning_nas_device")


class RadiusProfile(Base):
    """
    RADIUS profile defining authentication and authorization attributes.

    Profiles define speed limits, VLAN assignments, and other settings
    sent to NAS devices via RADIUS reply attributes.
    """
    __tablename__ = "radius_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]),
        default=NasVendor.other
    )
    connection_type: Mapped[ConnectionType | None] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x])
    )
    description: Mapped[str | None] = mapped_column(Text)

    # Bandwidth Settings (in Kbps)
    download_speed: Mapped[int | None] = mapped_column(Integer)  # Kbps
    upload_speed: Mapped[int | None] = mapped_column(Integer)    # Kbps
    burst_download: Mapped[int | None] = mapped_column(Integer)
    burst_upload: Mapped[int | None] = mapped_column(Integer)
    burst_threshold: Mapped[int | None] = mapped_column(Integer)
    burst_time: Mapped[int | None] = mapped_column(Integer)      # seconds

    # VLAN Settings
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    inner_vlan_id: Mapped[int | None] = mapped_column(Integer)   # QinQ

    # IP Pool Settings
    ip_pool_name: Mapped[str | None] = mapped_column(String(120))
    ipv6_pool_name: Mapped[str | None] = mapped_column(String(120))

    # Session Settings
    session_timeout: Mapped[int | None] = mapped_column(Integer)  # seconds
    idle_timeout: Mapped[int | None] = mapped_column(Integer)
    simultaneous_use: Mapped[int | None] = mapped_column(Integer, default=1)

    # MikroTik-specific (convenience fields)
    mikrotik_rate_limit: Mapped[str | None] = mapped_column(String(255))
    mikrotik_address_list: Mapped[str | None] = mapped_column(String(120))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    attributes = relationship("RadiusAttribute", back_populates="profile")
    offer_links = relationship("OfferRadiusProfile", back_populates="profile")
    access_credentials = relationship("AccessCredential", back_populates="radius_profile")
    subscriptions = relationship("Subscription", back_populates="radius_profile")


class RadiusAttribute(Base):
    __tablename__ = "radius_attributes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id"), nullable=False
    )
    attribute: Mapped[str] = mapped_column(String(120), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(255), nullable=False)

    profile = relationship("RadiusProfile", back_populates="attributes")


class OfferRadiusProfile(Base):
    __tablename__ = "offer_radius_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id"), nullable=False
    )

    offer = relationship("CatalogOffer", back_populates="radius_profiles")
    profile = relationship("RadiusProfile", back_populates="offer_links")


class AccessCredential(Base):
    __tablename__ = "access_credentials"
    __table_args__ = (UniqueConstraint("username", name="uq_access_credentials_username"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="access_credentials")
    radius_profile = relationship("RadiusProfile", back_populates="access_credentials")
    radius_users = relationship("RadiusUser", back_populates="access_credential")


# =============================================================================
# NAS CONFIGURATION BACKUP
# =============================================================================

class NasConfigBackup(Base):
    """
    Configuration backup for a NAS device.

    Stores full device configuration with version tracking
    for diff comparison and rollback capability.
    """
    __tablename__ = "nas_config_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    nas_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=False
    )

    # Configuration Content
    config_content: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str | None] = mapped_column(String(64))  # SHA256 hash
    config_format: Mapped[str | None] = mapped_column(String(40))  # rsc, txt, json
    config_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # Backup Metadata
    backup_method: Mapped[ConfigBackupMethod | None] = mapped_column(
        Enum(ConfigBackupMethod, values_callable=lambda x: [e.value for e in x])
    )
    is_scheduled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=True)

    # Change Detection
    has_changes: Mapped[bool] = mapped_column(Boolean, default=False)
    changes_summary: Mapped[str | None] = mapped_column(Text)

    # Status
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_by: Mapped[str | None] = mapped_column(String(120))

    # Relationships
    nas_device = relationship("NasDevice", back_populates="config_backups")


# =============================================================================
# PROVISIONING TEMPLATE
# =============================================================================

class ProvisioningTemplate(Base):
    """
    Provisioning script templates for different vendors and actions.

    Templates use placeholders like {{username}}, {{password}}, {{speed_down}}
    that are replaced with actual values during provisioning.
    """
    __tablename__ = "provisioning_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Template Identity
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80), unique=True)
    vendor: Mapped[NasVendor] = mapped_column(
        Enum(NasVendor, values_callable=lambda x: [e.value for e in x]),
        nullable=False
    )
    connection_type: Mapped[ConnectionType] = mapped_column(
        Enum(ConnectionType, values_callable=lambda x: [e.value for e in x]),
        nullable=False
    )
    action: Mapped[ProvisioningAction] = mapped_column(
        Enum(ProvisioningAction, values_callable=lambda x: [e.value for e in x]),
        nullable=False
    )

    # Template Content
    template_content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Available Placeholders (documented for UI)
    placeholders: Mapped[list | None] = mapped_column(JSONB)
    # Example: ["username", "password", "speed_down", "speed_up", "ip_address", "mac_address"]

    # Execution Settings
    execution_method: Mapped[ExecutionMethod | None] = mapped_column(
        Enum(ExecutionMethod, values_callable=lambda x: [e.value for e in x]),
    )
    expected_output: Mapped[str | None] = mapped_column(Text)  # regex pattern
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, default=30)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )


# =============================================================================
# PROVISIONING LOG
# =============================================================================

class ProvisioningLog(Base):
    """
    Log of all provisioning actions executed on NAS devices.

    Provides audit trail and troubleshooting capability.
    """
    __tablename__ = "provisioning_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provisioning_templates.id")
    )

    # Action Details
    action: Mapped[ProvisioningAction] = mapped_column(
        Enum(ProvisioningAction, values_callable=lambda x: [e.value for e in x]),
        nullable=False
    )
    command_sent: Mapped[str | None] = mapped_column(Text)
    response_received: Mapped[str | None] = mapped_column(Text)

    # Status
    status: Mapped[ProvisioningLogStatus] = mapped_column(
        Enum(ProvisioningLogStatus, values_callable=lambda x: [e.value for e in x]),
        default=ProvisioningLogStatus.pending,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer)

    # Context
    triggered_by: Mapped[str | None] = mapped_column(String(120))  # user or system
    request_data: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    # Relationships
    nas_device = relationship("NasDevice", back_populates="provisioning_logs")
    subscription = relationship("Subscription")
    template = relationship("ProvisioningTemplate")
