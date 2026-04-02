"""Tests for OLT CLI output parsers using TextFSM templates."""

from pathlib import Path

import pytest

from app.services.network.parsers import (
    parse_autofind,
    parse_key_value,
    parse_ont_info,
    parse_profile_table,
    parse_service_port_table,
)
from app.services.network.parsers.loader import clear_template_cache

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "huawei"


@pytest.fixture(autouse=True)
def reset_template_cache():
    """Clear template cache before each test."""
    clear_template_cache()


def _load_fixture(name: str) -> str:
    """Load a test fixture file."""
    fixture_path = FIXTURES_DIR / name
    if not fixture_path.exists():
        pytest.skip(f"Fixture not found: {fixture_path}")
    return fixture_path.read_text()


class TestAutofindParser:
    """Tests for display ont autofind all parser."""

    def test_parse_autofind_basic(self):
        """Parse autofind output with two ONTs."""
        output = _load_fixture("display_ont_autofind.txt")
        result = parse_autofind(output)

        assert result.success is True
        assert len(result.data) == 2
        assert result.row_count == 2

        # Check first entry
        entry1 = result.data[0]
        assert entry1.fsp == "0/2/1"
        assert entry1.serial_number == "HWTC-7D4733C3"
        assert entry1.serial_hex == "485754437D4733C3"
        assert entry1.vendor_id == "HWTC"
        assert entry1.model == "EG8145V5"
        assert entry1.software_version == "V5R020C00S115"

        # Check second entry
        entry2 = result.data[1]
        assert entry2.serial_number == "HWTC-AABBCCDD"
        assert entry2.mac == "AA-BB-CC-DD-EE-FF"
        assert entry2.equipment_sn == "HWTC123456789"

    def test_parse_autofind_empty(self):
        """Parse empty autofind output."""
        result = parse_autofind("")

        assert result.success is True
        assert len(result.data) == 0
        assert "Empty output" in result.warnings


class TestServicePortParser:
    """Tests for display service-port parser."""

    def test_parse_service_port_basic(self):
        """Parse service-port table with multiple entries."""
        output = _load_fixture("display_service_port.txt")
        result = parse_service_port_table(output)

        assert result.success is True
        assert len(result.data) == 5

        # Check first entry
        entry1 = result.data[0]
        assert entry1.index == 27
        assert entry1.vlan_id == 201
        assert entry1.ont_id == 0
        assert entry1.gem_index == 2
        assert entry1.flow_type == "vlan"
        assert entry1.flow_para == "201"
        assert entry1.state == "up"

        # Check entry with different state
        entry4 = result.data[3]
        assert entry4.index == 30
        assert entry4.state == "down"

    def test_parse_service_port_empty(self):
        """Parse empty service-port output."""
        result = parse_service_port_table("")

        assert result.success is True
        assert len(result.data) == 0


class TestProfileParser:
    """Tests for profile listing parser."""

    def test_parse_line_profiles(self):
        """Parse ont-lineprofile listing."""
        output = _load_fixture("display_ont_profile.txt")
        result = parse_profile_table(output)

        assert result.success is True
        assert len(result.data) == 4

        assert result.data[0].profile_id == 1
        assert result.data[0].name == "Default"
        assert result.data[1].profile_id == 2
        assert result.data[1].name == "FTTH-100M"

    def test_parse_tr069_profiles_with_binding_count(self):
        """Parse TR-069 profile listing with binding counts."""
        output = _load_fixture("display_tr069_profile.txt")
        result = parse_profile_table(output)

        assert result.success is True
        assert len(result.data) == 3

        assert result.data[0].profile_id == 1
        assert result.data[0].name == "DotMac-ACS"
        assert result.data[0].binding_count == 42

        assert result.data[1].binding_count == 0


class TestKeyValueParser:
    """Tests for key-value output parser."""

    def test_parse_tr069_profile_detail(self):
        """Parse TR-069 profile detail output."""
        output = _load_fixture("display_tr069_profile_detail.txt")
        result = parse_key_value(output)

        assert "profile-name" in result
        assert result["profile-name"] == "DotMac-ACS"
        assert result["url"] == "http://acs.dotmac.ng:7547"
        assert result["user name"] == "dotmac_cpe"
        assert result["inform interval"] == "300"
        assert result["binding times"] == "42"

    def test_parse_key_value_empty(self):
        """Parse empty key-value output."""
        result = parse_key_value("")
        assert result == {}


class TestOntInfoParser:
    """Tests for display ont info parser."""

    def test_parse_ont_info_basic(self):
        """Parse ont info table."""
        output = _load_fixture("display_ont_info.txt")
        result = parse_ont_info(output)

        assert result.success is True
        assert len(result.data) == 4

        # Check online ONT with description
        entry1 = result.data[0]
        assert entry1.fsp == "0/2/1"
        assert entry1.ont_id == 0
        assert entry1.serial_number == "HWTC7D4733C3"
        assert entry1.control_flag == "active"
        assert entry1.run_state == "online"
        assert entry1.config_state == "normal"
        assert entry1.match_state == "match"
        assert entry1.description == "Customer-001"

        # Check offline ONT
        entry3 = result.data[2]
        assert entry3.run_state == "offline"

        # Check mismatched ONT
        entry4 = result.data[3]
        assert entry4.match_state == "mismatch"


class TestParseResultConfidence:
    """Tests for ParseResult confidence calculation."""

    def test_confidence_with_markers(self):
        """Confidence should reflect extraction rate."""
        output = _load_fixture("display_ont_autofind.txt")
        result = parse_autofind(output)

        # Should have reasonable confidence since we extracted entries
        assert result.confidence > 0.0

    def test_confidence_empty_output(self):
        """Confidence should be 0 for empty output."""
        result = parse_autofind("")
        assert result.confidence == 0.0
