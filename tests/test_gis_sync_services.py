"""Tests for GIS sync services."""

from unittest.mock import MagicMock

from app.models.gis import GeoLocation, GeoLocationType
from app.models.network_monitoring import PopSite
from app.models.subscriber import Address
from app.services import gis_sync

# =============================================================================
# Helper Function Tests
# =============================================================================


class TestAddressDisplayName:
    """Tests for _address_display_name helper."""

    def test_with_label(self, db_session, subscriber):
        """Test address with label returns label."""
        address = Address(
            subscriber_id=subscriber.id,
            label="Home Address",
            address_line1="123 Main St",
            city="Test City",
            region="TC",
            postal_code="12345",
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync._address_display_name(address)
        assert result == "Home Address"

    def test_without_label_full_address(self, db_session, subscriber):
        """Test address without label returns formatted parts."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="456 Oak Ave",
            city="Oakland",
            region="CA",
            postal_code="94601",
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync._address_display_name(address)
        assert result == "456 Oak Ave, Oakland, CA, 94601"

    def test_without_label_partial_address(self, db_session, subscriber):
        """Test address without label with missing parts."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="789 Pine St",
            city="Portland",
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync._address_display_name(address)
        assert result == "789 Pine St, Portland"

    def test_without_label_only_line1(self, db_session, subscriber):
        """Test address with only address_line1."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="100 Main Street",
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync._address_display_name(address)
        assert result == "100 Main Street"


# =============================================================================
# SyncResult Tests
# =============================================================================


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_default_values(self):
        """Test SyncResult initializes with zeros."""
        result = gis_sync.SyncResult()
        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 0

    def test_custom_values(self):
        """Test SyncResult with custom values."""
        result = gis_sync.SyncResult(created=5, updated=3, skipped=2)
        assert result.created == 5
        assert result.updated == 3
        assert result.skipped == 2


# =============================================================================
# GeoSync.sync_pop_sites Tests
# =============================================================================


class TestSyncPopSites:
    """Tests for GeoSync.sync_pop_sites."""

    def test_creates_geo_location_for_pop_with_coords(self, db_session):
        """Test creating GeoLocation for PopSite with coordinates."""
        pop = PopSite(
            name="Downtown POP",
            code="DT-POP-1",
            latitude=40.7128,
            longitude=-74.0060,
            is_active=True,
        )
        db_session.add(pop)
        db_session.commit()

        result = gis_sync.GeoSync.sync_pop_sites(db_session)

        assert result.created == 1
        assert result.updated == 0
        assert result.skipped == 0

        geo_loc = db_session.query(GeoLocation).filter(
            GeoLocation.pop_site_id == pop.id
        ).first()
        assert geo_loc is not None
        assert geo_loc.name == "Downtown POP"
        assert geo_loc.location_type == GeoLocationType.pop
        assert geo_loc.latitude == 40.7128
        assert geo_loc.longitude == -74.0060

    def test_skips_pop_without_coords(self, db_session):
        """Test PopSite without coordinates is skipped."""
        pop = PopSite(
            name="Unknown Location POP",
            code="UNK-POP",
            latitude=None,
            longitude=None,
        )
        db_session.add(pop)
        db_session.commit()

        result = gis_sync.GeoSync.sync_pop_sites(db_session)

        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 1

    def test_updates_existing_geo_location(self, db_session):
        """Test updating existing GeoLocation for PopSite."""
        pop = PopSite(
            name="Original POP",
            code="OG-POP",
            latitude=40.0,
            longitude=-74.0,
            is_active=True,
        )
        db_session.add(pop)
        db_session.commit()

        # Create initial GeoLocation
        geo_loc = GeoLocation(
            name="Original POP",
            location_type=GeoLocationType.pop,
            latitude=40.0,
            longitude=-74.0,
            pop_site_id=pop.id,
            is_active=True,
        )
        db_session.add(geo_loc)
        db_session.commit()

        # Update the pop
        pop.name = "Renamed POP"
        pop.latitude = 41.0
        pop.longitude = -75.0
        db_session.commit()

        result = gis_sync.GeoSync.sync_pop_sites(db_session)

        assert result.created == 0
        assert result.updated == 1
        assert result.skipped == 0

        db_session.refresh(geo_loc)
        assert geo_loc.name == "Renamed POP"
        assert geo_loc.latitude == 41.0
        assert geo_loc.longitude == -75.0

    def test_deactivate_missing_pops(self, db_session):
        """Test deactivating GeoLocations for missing PopSites."""
        # Create an active pop
        pop = PopSite(
            name="Active POP",
            code="ACT-POP",
            latitude=40.0,
            longitude=-74.0,
            is_active=True,
        )
        db_session.add(pop)
        db_session.commit()

        # Create a pop that we'll "orphan" by removing coords
        orphan_pop = PopSite(
            name="Orphan POP",
            code="ORPHAN-POP",
            latitude=39.0,
            longitude=-73.0,
            is_active=True,
        )
        db_session.add(orphan_pop)
        db_session.commit()

        # Create GeoLocation for the orphan pop
        orphan_geo = GeoLocation(
            name="Orphan POP",
            location_type=GeoLocationType.pop,
            latitude=39.0,
            longitude=-73.0,
            pop_site_id=orphan_pop.id,
            is_active=True,
        )
        db_session.add(orphan_geo)
        db_session.commit()

        # Remove coordinates from orphan pop so it won't be in seen_ids
        orphan_pop.latitude = None
        orphan_pop.longitude = None
        db_session.commit()

        result = gis_sync.GeoSync.sync_pop_sites(db_session, deactivate_missing=True)

        assert result.created == 1  # For the active pop
        assert result.skipped == 1  # For the orphan pop (no coords)

        db_session.refresh(orphan_geo)
        assert orphan_geo.is_active is False

    def test_multiple_pops_mixed_results(self, db_session):
        """Test sync with multiple pops having different states."""
        pop_with_coords = PopSite(
            name="With Coords",
            code="W-COORD",
            latitude=40.0,
            longitude=-74.0,
        )
        pop_without_lat = PopSite(
            name="No Lat",
            code="NO-LAT",
            latitude=None,
            longitude=-74.0,
        )
        pop_without_lon = PopSite(
            name="No Lon",
            code="NO-LON",
            latitude=40.0,
            longitude=None,
        )
        db_session.add_all([pop_with_coords, pop_without_lat, pop_without_lon])
        db_session.commit()

        result = gis_sync.GeoSync.sync_pop_sites(db_session)

        assert result.created == 1
        assert result.skipped == 2


# =============================================================================
# GeoSync.sync_addresses Tests
# =============================================================================


class TestSyncAddresses:
    """Tests for GeoSync.sync_addresses."""

    def test_creates_geo_location_for_address_with_coords(self, db_session, subscriber):
        """Test creating GeoLocation for Address with coordinates."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Main St",
            city="Test City",
            region="TS",
            postal_code="12345",
            latitude=40.7128,
            longitude=-74.0060,
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync.GeoSync.sync_addresses(db_session)

        assert result.created == 1
        assert result.updated == 0
        assert result.skipped == 0

        geo_loc = db_session.query(GeoLocation).filter(
            GeoLocation.address_id == address.id
        ).first()
        assert geo_loc is not None
        assert geo_loc.location_type == GeoLocationType.address
        assert geo_loc.latitude == 40.7128

    def test_skips_address_without_coords(self, db_session, subscriber):
        """Test Address without coordinates is skipped."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="Unknown Address",
            latitude=None,
            longitude=None,
        )
        db_session.add(address)
        db_session.commit()

        result = gis_sync.GeoSync.sync_addresses(db_session)

        assert result.created == 0
        assert result.skipped == 1

    def test_updates_existing_geo_location(self, db_session, subscriber):
        """Test updating existing GeoLocation for Address."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="Original Address",
            city="Old City",
            latitude=40.0,
            longitude=-74.0,
        )
        db_session.add(address)
        db_session.commit()

        # Create initial GeoLocation
        geo_loc = GeoLocation(
            name="Original Address, Old City",
            location_type=GeoLocationType.address,
            latitude=40.0,
            longitude=-74.0,
            address_id=address.id,
            is_active=True,
        )
        db_session.add(geo_loc)
        db_session.commit()

        # Update the address
        address.address_line1 = "New Address"
        address.city = "New City"
        address.latitude = 41.0
        address.longitude = -75.0
        db_session.commit()

        result = gis_sync.GeoSync.sync_addresses(db_session)

        assert result.created == 0
        assert result.updated == 1

        db_session.refresh(geo_loc)
        assert geo_loc.name == "New Address, New City"
        assert geo_loc.latitude == 41.0

    def test_deactivate_missing_addresses(self, db_session, subscriber):
        """Test deactivating GeoLocations for missing addresses."""
        # Create an active address with coords
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="Active Address",
            latitude=40.0,
            longitude=-74.0,
        )
        db_session.add(address)
        db_session.commit()

        # Create an address that we'll "orphan" by removing coords
        orphan_address = Address(
            subscriber_id=subscriber.id,
            address_line1="Orphan Address",
            latitude=39.0,
            longitude=-73.0,
        )
        db_session.add(orphan_address)
        db_session.commit()

        # Create GeoLocation for the orphan address
        orphan_geo = GeoLocation(
            name="Orphan Address",
            location_type=GeoLocationType.address,
            latitude=39.0,
            longitude=-73.0,
            address_id=orphan_address.id,
            is_active=True,
        )
        db_session.add(orphan_geo)
        db_session.commit()

        # Remove coordinates from orphan address so it won't be in seen_ids
        orphan_address.latitude = None
        orphan_address.longitude = None
        db_session.commit()

        result = gis_sync.GeoSync.sync_addresses(db_session, deactivate_missing=True)

        assert result.created == 1  # For the active address
        assert result.skipped == 1  # For the orphan address (no coords)

        db_session.refresh(orphan_geo)
        assert orphan_geo.is_active is False


# =============================================================================
# GeoSync.run_sync Tests
# =============================================================================


class TestRunSync:
    """Tests for GeoSync.run_sync."""

    def test_sync_pops_only(self, db_session):
        """Test running sync for pops only."""
        pop = PopSite(
            name="Test POP",
            code="TEST-POP",
            latitude=40.0,
            longitude=-74.0,
        )
        db_session.add(pop)
        db_session.commit()

        results = gis_sync.GeoSync.run_sync(
            db_session,
            sync_pops=True,
            sync_addresses=False,
            deactivate_missing=False,
        )

        assert "pop_sites" in results
        assert "addresses" not in results
        assert results["pop_sites"]["created"] == 1

    def test_sync_addresses_only(self, db_session, subscriber):
        """Test running sync for addresses only."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="Test Address",
            latitude=40.0,
            longitude=-74.0,
        )
        db_session.add(address)
        db_session.commit()

        results = gis_sync.GeoSync.run_sync(
            db_session,
            sync_pops=False,
            sync_addresses=True,
            deactivate_missing=False,
        )

        assert "pop_sites" not in results
        assert "addresses" in results
        assert results["addresses"]["created"] == 1

    def test_sync_both(self, db_session, subscriber):
        """Test running sync for both pops and addresses."""
        pop = PopSite(
            name="Test POP",
            code="BOTH-POP",
            latitude=40.0,
            longitude=-74.0,
        )
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="Test Address",
            latitude=41.0,
            longitude=-75.0,
        )
        db_session.add_all([pop, address])
        db_session.commit()

        results = gis_sync.GeoSync.run_sync(
            db_session,
            sync_pops=True,
            sync_addresses=True,
            deactivate_missing=False,
        )

        assert "pop_sites" in results
        assert "addresses" in results
        assert results["pop_sites"]["created"] == 1
        assert results["addresses"]["created"] == 1

    def test_sync_none(self, db_session):
        """Test running sync with nothing enabled."""
        results = gis_sync.GeoSync.run_sync(
            db_session,
            sync_pops=False,
            sync_addresses=False,
            deactivate_missing=False,
        )

        assert results == {}


# =============================================================================
# GeoSync.sync_sources Tests
# =============================================================================


class TestSyncSources:
    """Tests for GeoSync.sync_sources."""

    def test_sync_sources_foreground(self, db_session):
        """Test sync_sources in foreground mode."""
        pop = PopSite(
            name="FG Test POP",
            code="FG-POP",
            latitude=40.0,
            longitude=-74.0,
        )
        db_session.add(pop)
        db_session.commit()

        background_tasks = MagicMock()

        results = gis_sync.GeoSync.sync_sources(
            db_session,
            background_tasks,
            sync_pops=True,
            sync_addresses=False,
            deactivate_missing=False,
            background=False,
        )

        assert "pop_sites" in results
        background_tasks.add_task.assert_not_called()

    def test_sync_sources_background(self, db_session):
        """Test sync_sources in background mode."""
        background_tasks = MagicMock()

        results = gis_sync.GeoSync.sync_sources(
            db_session,
            background_tasks,
            sync_pops=True,
            sync_addresses=False,
            deactivate_missing=False,
            background=True,
        )

        assert results == {"status": "queued"}
        background_tasks.add_task.assert_called_once()


# =============================================================================
# GeoSync.queue_sync Tests
# =============================================================================


class TestQueueSync:
    """Tests for GeoSync.queue_sync."""

    def test_queue_sync_returns_queued_status(self):
        """Test queue_sync returns queued status."""
        background_tasks = MagicMock()

        result = gis_sync.GeoSync.queue_sync(
            background_tasks,
            sync_pops=True,
            sync_addresses=True,
            deactivate_missing=False,
        )

        assert result == {"status": "queued"}
        background_tasks.add_task.assert_called_once()

    def test_queue_sync_adds_task(self):
        """Test queue_sync adds a task to background_tasks."""
        background_tasks = MagicMock()

        gis_sync.GeoSync.queue_sync(
            background_tasks,
            sync_pops=True,
            sync_addresses=False,
            deactivate_missing=True,
        )

        # Verify add_task was called with a callable
        args, kwargs = background_tasks.add_task.call_args
        assert callable(args[0])


# =============================================================================
# Module Instance Tests
# =============================================================================


def test_geo_sync_module_instance():
    """Test geo_sync module instance exists."""
    assert gis_sync.geo_sync is not None
    assert isinstance(gis_sync.geo_sync, gis_sync.GeoSync)
