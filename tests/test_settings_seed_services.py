"""Tests for settings seed services."""



from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import settings_seed

# =============================================================================
# Auth Settings Tests
# =============================================================================


class TestSeedAuthSettings:
    """Tests for seed_auth_settings function."""

    def test_seeds_jwt_algorithm(self, db_session, monkeypatch):
        """Test JWT algorithm setting is seeded."""
        monkeypatch.setenv("JWT_ALGORITHM", "RS256")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.auth,
            DomainSetting.key == "jwt_algorithm",
        ).first()
        assert setting is not None
        assert setting.value_text == "RS256"

    def test_seeds_jwt_ttl(self, db_session, monkeypatch):
        """Test JWT TTL settings are seeded."""
        monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "30")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.auth,
            DomainSetting.key == "jwt_access_ttl_minutes",
        ).first()
        assert setting is not None
        assert setting.value_text == "30"

    def test_seeds_refresh_cookie_settings(self, db_session, monkeypatch):
        """Test refresh cookie settings are seeded."""
        monkeypatch.setenv("REFRESH_COOKIE_NAME", "custom_refresh")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.auth,
            DomainSetting.key == "refresh_cookie_name",
        ).first()
        assert setting is not None
        assert setting.value_text == "custom_refresh"


# =============================================================================
# Audit Settings Tests
# =============================================================================


class TestSeedAuditSettings:
    """Tests for seed_audit_settings function."""

    def test_seeds_audit_enabled(self, db_session):
        """Test audit enabled setting is seeded."""
        settings_seed.seed_audit_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.audit,
            DomainSetting.key == "enabled",
        ).first()
        assert setting is not None
        assert setting.value_json is True

    def test_seeds_audit_methods(self, db_session):
        """Test audit methods setting is seeded."""
        settings_seed.seed_audit_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.audit,
            DomainSetting.key == "methods",
        ).first()
        assert setting is not None
        assert "POST" in setting.value_json


# =============================================================================
# Imports Settings Tests
# =============================================================================


class TestSeedImportsSettings:
    """Tests for seed_imports_settings function."""

    def test_seeds_max_file_bytes(self, db_session):
        """Test max file bytes setting is seeded."""
        settings_seed.seed_imports_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.imports,
            DomainSetting.key == "max_file_bytes",
        ).first()
        assert setting is not None
        assert int(setting.value_text) == 5 * 1024 * 1024


# =============================================================================
# GIS Settings Tests
# =============================================================================


class TestSeedGisSettings:
    """Tests for seed_gis_settings function."""

    def test_seeds_sync_enabled(self, db_session):
        """Test GIS sync enabled setting is seeded."""
        settings_seed.seed_gis_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.gis,
            DomainSetting.key == "sync_enabled",
        ).first()
        assert setting is not None
        assert setting.value_json is True

    def test_seeds_sync_interval(self, db_session):
        """Test GIS sync interval setting is seeded."""
        settings_seed.seed_gis_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.gis,
            DomainSetting.key == "sync_interval_minutes",
        ).first()
        assert setting is not None
        assert setting.value_text == "60"


# =============================================================================
# Usage Settings Tests
# =============================================================================


class TestSeedUsageSettings:
    """Tests for seed_usage_settings function."""

    def test_seeds_usage_rating_enabled(self, db_session, monkeypatch):
        """Test usage rating enabled setting is seeded."""
        monkeypatch.setenv("USAGE_RATING_ENABLED", "true")

        settings_seed.seed_usage_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.usage,
            DomainSetting.key == "usage_rating_enabled",
        ).first()
        assert setting is not None
        assert setting.value_json is True


# =============================================================================
# Notification Settings Tests
# =============================================================================


class TestSeedNotificationSettings:
    """Tests for seed_notification_settings function."""

    def test_seeds_alert_notifications_enabled(self, db_session, monkeypatch):
        """Test alert notifications enabled setting is seeded."""
        monkeypatch.setenv("ALERT_NOTIFICATIONS_ENABLED", "true")
        # Set all required env vars to avoid validation errors
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_CHANNEL", "email")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT", "admin@example.com")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID", "tmpl-1")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID", "rot-1")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES", "5")

        settings_seed.seed_notification_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.notification,
            DomainSetting.key == "alert_notifications_enabled",
        ).first()
        assert setting is not None


# =============================================================================
# Collections Settings Tests
# =============================================================================


class TestSeedCollectionsSettings:
    """Tests for seed_collections_settings function."""

    def test_seeds_dunning_enabled(self, db_session, monkeypatch):
        """Test dunning enabled setting is seeded."""
        monkeypatch.setenv("DUNNING_ENABLED", "false")

        settings_seed.seed_collections_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.collections,
            DomainSetting.key == "dunning_enabled",
        ).first()
        assert setting is not None
        assert setting.value_json is False

    def test_seeds_prepaid_skip_holidays(self, db_session, monkeypatch):
        """Test prepaid skip holidays setting is seeded."""
        monkeypatch.setenv("PREPAID_SKIP_HOLIDAYS", '["2026-01-01"]')

        settings_seed.seed_collections_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.collections,
            DomainSetting.key == "prepaid_skip_holidays",
        ).first()
        assert setting is not None
        assert setting.value_json == ["2026-01-01"]


# =============================================================================
# Geocoding Settings Tests
# =============================================================================


class TestSeedGeocodingSettings:
    """Tests for seed_geocoding_settings function."""

    def test_seeds_geocoding_enabled(self, db_session, monkeypatch):
        """Test geocoding enabled setting is seeded."""
        # Set all required env vars to avoid None value_text
        monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")
        monkeypatch.setenv("GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org")
        monkeypatch.setenv("GEOCODING_USER_AGENT", "dotmac_sm")
        monkeypatch.setenv("GEOCODING_EMAIL", "test@example.com")
        monkeypatch.setenv("GEOCODING_TIMEOUT_SEC", "5")

        settings_seed.seed_geocoding_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.geocoding,
            DomainSetting.key == "enabled",
        ).first()
        assert setting is not None
        assert setting.value_json is True

    def test_seeds_geocoding_provider(self, db_session, monkeypatch):
        """Test geocoding provider setting is seeded."""
        monkeypatch.setenv("GEOCODING_PROVIDER", "google")
        monkeypatch.setenv("GEOCODING_BASE_URL", "https://maps.googleapis.com")
        monkeypatch.setenv("GEOCODING_USER_AGENT", "dotmac_sm")
        monkeypatch.setenv("GEOCODING_EMAIL", "test@example.com")
        monkeypatch.setenv("GEOCODING_TIMEOUT_SEC", "5")

        settings_seed.seed_geocoding_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.geocoding,
            DomainSetting.key == "provider",
        ).first()
        assert setting is not None
        assert setting.value_text == "google"


# =============================================================================
# Scheduler Settings Tests
# =============================================================================


class TestSeedSchedulerSettings:
    """Tests for seed_scheduler_settings function."""

    def test_seeds_broker_url(self, db_session, monkeypatch):
        """Test broker URL setting is seeded."""
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://custom:6379/0")
        monkeypatch.delenv("REDIS_URL", raising=False)

        settings_seed.seed_scheduler_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.scheduler,
            DomainSetting.key == "broker_url",
        ).first()
        assert setting is not None
        assert "redis://" in setting.value_text

    def test_seeds_timezone(self, db_session, monkeypatch):
        """Test timezone setting is seeded."""
        monkeypatch.setenv("CELERY_TIMEZONE", "America/New_York")

        settings_seed.seed_scheduler_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.scheduler,
            DomainSetting.key == "timezone",
        ).first()
        assert setting is not None
        assert setting.value_text == "America/New_York"


# =============================================================================
# Radius Settings Tests
# =============================================================================


class TestSeedRadiusSettings:
    """Tests for seed_radius_settings function."""

    def test_seeds_radius_auth_settings(self, db_session, monkeypatch):
        """Test RADIUS auth settings are seeded."""
        monkeypatch.setenv("RADIUS_AUTH_SERVER_ID", "server-123")
        monkeypatch.setenv("RADIUS_AUTH_SHARED_SECRET", "secret123")
        monkeypatch.setenv("RADIUS_AUTH_DICTIONARY", "/etc/raddb/dictionary")
        monkeypatch.setenv("RADIUS_AUTH_TIMEOUT_SEC", "3")

        settings_seed.seed_radius_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.radius,
            DomainSetting.key == "auth_server_id",
        ).first()
        assert setting is not None


# =============================================================================
# Billing Settings Tests
# =============================================================================


class TestSeedBillingSettings:
    """Tests for seed_billing_settings function."""

    def test_seeds_default_currency(self, db_session, monkeypatch):
        """Test default currency setting is seeded."""
        monkeypatch.setenv("BILLING_DEFAULT_CURRENCY", "EUR")

        settings_seed.seed_billing_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.billing,
            DomainSetting.key == "default_currency",
        ).first()
        assert setting is not None
        assert setting.value_text == "EUR"

    def test_seeds_invoice_number_settings(self, db_session, monkeypatch):
        """Test invoice number settings are seeded."""
        monkeypatch.setenv("BILLING_INVOICE_NUMBER_PREFIX", "INVOICE-")

        settings_seed.seed_billing_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.billing,
            DomainSetting.key == "invoice_number_prefix",
        ).first()
        assert setting is not None
        assert setting.value_text == "INVOICE-"


# =============================================================================
# Catalog Settings Tests
# =============================================================================


class TestSeedCatalogSettings:
    """Tests for seed_catalog_settings function."""

    def test_seeds_default_proration_policy(self, db_session, monkeypatch):
        """Test default proration policy is seeded."""
        monkeypatch.setenv("CATALOG_DEFAULT_PRORATION_POLICY", "none")

        settings_seed.seed_catalog_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.catalog,
            DomainSetting.key == "default_proration_policy",
        ).first()
        assert setting is not None
        assert setting.value_text == "none"


# =============================================================================
# Subscriber Settings Tests
# =============================================================================


class TestSeedSubscriberSettings:
    """Tests for seed_subscriber_settings function."""

    def test_seeds_default_account_status(self, db_session, monkeypatch):
        """Test default account status is seeded."""
        monkeypatch.setenv("SUBSCRIBER_DEFAULT_ACCOUNT_STATUS", "pending")

        settings_seed.seed_subscriber_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.subscriber,
            DomainSetting.key == "default_account_status",
        ).first()
        assert setting is not None
        assert setting.value_text == "pending"


# =============================================================================
# Usage Policy Settings Tests
# =============================================================================


class TestSeedUsagePolicySettings:
    """Tests for seed_usage_policy_settings function."""

    def test_seeds_default_charge_status(self, db_session, monkeypatch):
        """Test default charge status is seeded."""
        monkeypatch.setenv("USAGE_DEFAULT_CHARGE_STATUS", "pending")

        settings_seed.seed_usage_policy_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.usage,
            DomainSetting.key == "default_charge_status",
        ).first()
        assert setting is not None


# =============================================================================
# Collections Policy Settings Tests
# =============================================================================


class TestSeedCollectionsPolicySettings:
    """Tests for seed_collections_policy_settings function."""

    def test_seeds_default_dunning_case_status(self, db_session, monkeypatch):
        """Test default dunning case status is seeded."""
        monkeypatch.setenv("COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS", "active")

        settings_seed.seed_collections_policy_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.collections,
            DomainSetting.key == "default_dunning_case_status",
        ).first()
        assert setting is not None


# =============================================================================
# Auth Policy Settings Tests
# =============================================================================


class TestSeedAuthPolicySettings:
    """Tests for seed_auth_policy_settings function."""

    def test_seeds_default_auth_provider(self, db_session, monkeypatch):
        """Test default auth provider is seeded."""
        monkeypatch.setenv("AUTH_DEFAULT_AUTH_PROVIDER", "ldap")

        settings_seed.seed_auth_policy_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.auth,
            DomainSetting.key == "default_auth_provider",
        ).first()
        assert setting is not None
        assert setting.value_text == "ldap"


# =============================================================================
# Provisioning Settings Tests
# =============================================================================


class TestSeedProvisioningSettings:
    """Tests for seed_provisioning_settings function."""

    def test_seeds_default_service_order_status(self, db_session, monkeypatch):
        """Test default service order status is seeded."""
        monkeypatch.setenv("PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS", "pending")

        settings_seed.seed_provisioning_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.provisioning,
            DomainSetting.key == "default_service_order_status",
        ).first()
        assert setting is not None


# =============================================================================
# Projects Settings Tests
# =============================================================================


class TestSeedProjectsSettings:
    """Tests for seed_projects_settings function."""

    def test_seeds_default_project_status(self, db_session, monkeypatch):
        """Test default project status is seeded."""
        monkeypatch.setenv("PROJECTS_DEFAULT_PROJECT_STATUS", "active")

        settings_seed.seed_projects_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.projects,
            DomainSetting.key == "default_project_status",
        ).first()
        assert setting is not None


# =============================================================================
# Network Policy Settings Tests
# =============================================================================


class TestSeedNetworkPolicySettings:
    """Tests for seed_network_policy_settings function."""

    def test_seeds_default_device_type(self, db_session, monkeypatch):
        """Test default device type is seeded."""
        monkeypatch.setenv("NETWORK_DEFAULT_DEVICE_TYPE", "router")

        settings_seed.seed_network_policy_settings(db_session)

        from app.models.domain_settings import SettingDomain

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.network,
            DomainSetting.key == "default_device_type",
        ).first()
        assert setting is not None


# =============================================================================
# Radius Policy Settings Tests
# =============================================================================


class TestSeedRadiusPolicySettings:
    """Tests for seed_radius_policy_settings function."""

    def test_seeds_default_auth_port(self, db_session, monkeypatch):
        """Test default auth port is seeded."""
        monkeypatch.setenv("RADIUS_DEFAULT_AUTH_PORT", "1645")

        settings_seed.seed_radius_policy_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.radius,
            DomainSetting.key == "default_auth_port",
        ).first()
        assert setting is not None
        assert setting.value_text == "1645"


# =============================================================================
# Inventory Settings Tests
# =============================================================================


class TestSeedInventorySettings:
    """Tests for seed_inventory_settings function."""

    def test_seeds_default_reservation_status(self, db_session, monkeypatch):
        """Test default reservation status is seeded."""
        monkeypatch.setenv("INVENTORY_DEFAULT_RESERVATION_STATUS", "pending")

        settings_seed.seed_inventory_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.inventory,
            DomainSetting.key == "default_reservation_status",
        ).first()
        assert setting is not None


# =============================================================================
# Lifecycle Settings Tests
# =============================================================================


class TestSeedLifecycleSettings:
    """Tests for seed_lifecycle_settings function."""

    def test_seeds_default_event_type(self, db_session, monkeypatch):
        """Test default event type is seeded."""
        monkeypatch.setenv("LIFECYCLE_DEFAULT_EVENT_TYPE", "create")

        settings_seed.seed_lifecycle_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.lifecycle,
            DomainSetting.key == "default_event_type",
        ).first()
        assert setting is not None


# =============================================================================
# Comms Settings Tests
# =============================================================================


class TestSeedCommsSettings:
    """Tests for seed_comms_settings function."""

    def test_seeds_default_notification_status(self, db_session, monkeypatch):
        """Test default notification status is seeded."""
        monkeypatch.setenv("COMMS_DEFAULT_NOTIFICATION_STATUS", "sent")

        settings_seed.seed_comms_settings(db_session)

        setting = db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.comms,
            DomainSetting.key == "default_notification_status",
        ).first()
        assert setting is not None
