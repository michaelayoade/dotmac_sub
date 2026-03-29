# ONT DDM & SNMP Polling Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ONT DDM health telemetry (temperature, voltage, bias current, Tx power), SNMP-based serial number retrieval, and offline reason polling to the existing OLT polling pipeline.

**Architecture:** Extend the existing `OntSignalReading` dataclass and `poll_olt_ont_signals()` function to walk additional Huawei/ZTE/Nokia OID tables during each poll cycle. New fields are added to `OntUnit` model via migration. DDM health metrics are pushed to VictoriaMetrics alongside existing signal metrics. A new `ont.ddm_alert` event fires when temperature or voltage exceeds thresholds.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0, Alembic, pytest, SNMP (snmpbulkwalk/snmpwalk CLI)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/models/network.py` | Modify | Add DDM fields to `OntUnit` |
| `app/services/network/olt_polling.py` | Modify | Add DDM OIDs, extend reading/parsing/update logic |
| `app/services/events/types.py` | Modify | Add `ont.ddm_alert` event type |
| `alembic/versions/XXX_add_ont_ddm_fields.py` | Create | Migration for new columns |
| `tests/test_olt_polling_service.py` | Modify | Tests for new parsing, DDM polling logic |

---

### Task 1: Add DDM columns to OntUnit model

**Files:**
- Modify: `app/models/network.py:910-914` (after existing signal fields)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py — add at top-level

def test_ont_unit_has_ddm_fields() -> None:
    """OntUnit model must expose DDM health columns."""
    from app.models.network import OntUnit

    for field_name in [
        "onu_tx_signal_dbm",
        "ont_temperature_c",
        "ont_voltage_v",
        "ont_bias_current_ma",
    ]:
        assert hasattr(OntUnit, field_name), f"OntUnit missing field: {field_name}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_olt_polling_service.py::test_ont_unit_has_ddm_fields -v`
Expected: FAIL — `OntUnit missing field: onu_tx_signal_dbm`

- [ ] **Step 3: Add DDM columns to OntUnit**

In `app/models/network.py`, after line 913 (`distance_meters`), add:

```python
    # ONT DDM health telemetry (SNMP-polled)
    onu_tx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    ont_temperature_c: Mapped[float | None] = mapped_column(Float)
    ont_voltage_v: Mapped[float | None] = mapped_column(Float)
    ont_bias_current_ma: Mapped[float | None] = mapped_column(Float)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_olt_polling_service.py::test_ont_unit_has_ddm_fields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/models/network.py tests/test_olt_polling_service.py
git commit -m "feat: add DDM health telemetry columns to OntUnit model"
```

---

### Task 2: Create Alembic migration for DDM columns

**Files:**
- Create: `alembic/versions/d1d2d3d4d5d6_add_ont_ddm_fields.py`

- [ ] **Step 1: Generate migration**

```bash
cd /home/dotmac/projects/dotmac_sub
make migrate-new msg="add ont ddm health telemetry fields"
```

- [ ] **Step 2: Edit the generated migration to be idempotent**

Replace the generated `upgrade()` and `downgrade()` with:

```python
def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "onu_tx_signal_dbm" not in columns:
        op.add_column("ont_units", sa.Column("onu_tx_signal_dbm", sa.Float(), nullable=True))
    if "ont_temperature_c" not in columns:
        op.add_column("ont_units", sa.Column("ont_temperature_c", sa.Float(), nullable=True))
    if "ont_voltage_v" not in columns:
        op.add_column("ont_units", sa.Column("ont_voltage_v", sa.Float(), nullable=True))
    if "ont_bias_current_ma" not in columns:
        op.add_column("ont_units", sa.Column("ont_bias_current_ma", sa.Float(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    for col in ["ont_bias_current_ma", "ont_voltage_v", "ont_temperature_c", "onu_tx_signal_dbm"]:
        if col in columns:
            op.drop_column("ont_units", col)
```

- [ ] **Step 3: Run migration**

```bash
make migrate
```
Expected: Migration applies cleanly.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/*_add_ont_ddm_health_telemetry_fields.py
git commit -m "migrate: add ont_ddm health telemetry columns to ont_units"
```

---

### Task 3: Add DDM OIDs to vendor OID maps

**Files:**
- Modify: `app/services/network/olt_polling.py:42-91`
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py

def test_vendor_oids_include_ddm_keys() -> None:
    """All vendor OID maps must include DDM OID keys."""
    from app.services.network.olt_polling import _VENDOR_OID_OIDS, GENERIC_OIDS

    ddm_keys = {"onu_tx", "temperature", "voltage", "bias_current"}
    for vendor, oids in _VENDOR_OID_OIDS.items():
        for key in ddm_keys:
            assert key in oids, f"Vendor '{vendor}' missing OID key: {key}"
    for key in ddm_keys:
        assert key in GENERIC_OIDS, f"GENERIC_OIDS missing OID key: {key}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_olt_polling_service.py::test_vendor_oids_include_ddm_keys -v`
Expected: FAIL — missing `onu_tx` key

- [ ] **Step 3: Add DDM OIDs to all vendor maps**

In `app/services/network/olt_polling.py`, update `_VENDOR_OID_OIDS`:

```python
_VENDOR_OID_OIDS: dict[str, dict[str, str]] = {
    "huawei": {
        # hwGponOltOpticsDdmInfoRxPower — OLT receive power per ONU
        "olt_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        # hwGponOltOpticsDdmInfoTxPower — ONU receive (reported via OLT)
        "onu_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
        # hwGponOntOpticalDdmTxPower — ONU transmit power
        "onu_tx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.2",
        # hwGponOntOpticalDdmTemperature — ONU laser temperature
        "temperature": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.3",
        # hwGponOntOpticalDdmBiasCurrent — ONU laser bias current
        "bias_current": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.5",
        # hwGponOntOpticalDdmVoltage — ONU supply voltage
        "voltage": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.7",
        # hwGponOltEponOnuDistance
        "distance": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
        # hwGponDeviceOnuRunStatus
        "status": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
        # hwGponDeviceOntLastDownCause
        "offline_reason": ".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.12",
        # hwGponDeviceOntSN
        "serial_number": ".1.3.6.1.4.1.2011.6.128.1.1.2.43.1.2",
    },
    "zte": {
        "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
        "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
        "onu_tx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.4",
        "temperature": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.5",
        "bias_current": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.8",
        "voltage": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.6",
        "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
        "offline_reason": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.11",
        "serial_number": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.3",
    },
    "nokia": {
        "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
        "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
        "onu_tx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.3",
        "temperature": ".1.3.6.1.4.1.637.61.1.35.10.14.1.5",
        "bias_current": ".1.3.6.1.4.1.637.61.1.35.10.14.1.7",
        "voltage": ".1.3.6.1.4.1.637.61.1.35.10.14.1.6",
        "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
        "offline_reason": ".1.3.6.1.4.1.637.61.1.35.10.1.1.10",
        "serial_number": ".1.3.6.1.4.1.637.61.1.35.10.1.1.3",
    },
}
```

Update `GENERIC_OIDS`:

```python
GENERIC_OIDS: dict[str, str] = {
    "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
    "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
    "onu_tx": ".1.3.6.1.4.1.17409.2.3.6.10.1.4",
    "temperature": ".1.3.6.1.4.1.17409.2.3.6.10.1.5",
    "bias_current": ".1.3.6.1.4.1.17409.2.3.6.10.1.7",
    "voltage": ".1.3.6.1.4.1.17409.2.3.6.10.1.6",
    "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
    "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_olt_polling_service.py::test_vendor_oids_include_ddm_keys -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/network/olt_polling.py tests/test_olt_polling_service.py
git commit -m "feat: add DDM and offline_reason OIDs for all vendors"
```

---

### Task 4: Extend OntSignalReading with DDM fields

**Files:**
- Modify: `app/services/network/olt_polling.py:178-186`
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py

def test_ont_signal_reading_has_ddm_fields() -> None:
    """OntSignalReading must include DDM health fields."""
    from app.services.network.olt_polling import OntSignalReading

    reading = OntSignalReading(
        onu_index="0.1.3.5",
        olt_rx_dbm=-19.5,
        onu_rx_dbm=-21.0,
        onu_tx_dbm=2.5,
        distance_m=1200,
        is_online=True,
        temperature_c=42.0,
        voltage_v=3.3,
        bias_current_ma=15.2,
        offline_reason_raw=None,
        serial_number_raw=None,
    )
    assert reading.onu_tx_dbm == 2.5
    assert reading.temperature_c == 42.0
    assert reading.voltage_v == 3.3
    assert reading.bias_current_ma == 15.2
    assert reading.offline_reason_raw is None
    assert reading.serial_number_raw is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_olt_polling_service.py::test_ont_signal_reading_has_ddm_fields -v`
Expected: FAIL — unexpected keyword argument `onu_tx_dbm`

- [ ] **Step 3: Extend OntSignalReading dataclass**

In `app/services/network/olt_polling.py`, replace the `OntSignalReading` dataclass:

```python
@dataclass(frozen=True)
class OntSignalReading:
    """Signal reading for a single ONT from SNMP poll."""

    onu_index: str
    olt_rx_dbm: float | None
    onu_rx_dbm: float | None
    onu_tx_dbm: float | None
    distance_m: int | None
    is_online: bool | None
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None
    offline_reason_raw: str | None = None
    serial_number_raw: str | None = None
```

- [ ] **Step 4: Fix all existing call sites that construct OntSignalReading**

In the same file, in `poll_olt_ont_signals()` around line 700, update the reading construction to pass `onu_tx_dbm=None` (will be wired to real data in Task 5):

```python
        readings.append(
            OntSignalReading(
                onu_index=idx,
                olt_rx_dbm=_parse_signal_value(
                    olt_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="olt_rx",
                    stats=parse_stats,
                ),
                onu_rx_dbm=_parse_signal_value(
                    onu_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_rx",
                    stats=parse_stats,
                ),
                onu_tx_dbm=None,
                distance_m=_parse_distance(distance_raw.get(idx, "")),
                is_online=_parse_online_status(status_raw.get(idx, "")),
            )
        )
```

- [ ] **Step 5: Run all existing tests to verify nothing broke**

Run: `pytest tests/test_olt_polling_service.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/network/olt_polling.py tests/test_olt_polling_service.py
git commit -m "feat: extend OntSignalReading with DDM health fields"
```

---

### Task 5: Wire DDM SNMP walks into poll_olt_ont_signals

**Files:**
- Modify: `app/services/network/olt_polling.py` (function `poll_olt_ont_signals`, ~lines 622-922)
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write parsing tests for DDM values**

```python
# tests/test_olt_polling_service.py

def test_parse_signal_value_onu_tx() -> None:
    """ONU Tx power should parse like OLT Rx (simple scale)."""
    from app.services.network.olt_polling import _parse_signal_value

    value = _parse_signal_value("250", vendor="huawei", metric="onu_tx")
    assert value == 2.5


def test_parse_temperature_value() -> None:
    """Temperature values are returned as integer degrees C."""
    from app.services.network.olt_polling import _parse_ddm_value

    assert _parse_ddm_value("42") == 42.0
    assert _parse_ddm_value("") is None
    assert _parse_ddm_value("No Such Instance") is None


def test_parse_voltage_value() -> None:
    """Voltage values are in 0.01V units for Huawei."""
    from app.services.network.olt_polling import _parse_ddm_value

    # 330 = 3.30V
    assert _parse_ddm_value("330", scale=0.01) == 3.3


def test_parse_bias_current_value() -> None:
    """Bias current in 0.001 mA units."""
    from app.services.network.olt_polling import _parse_ddm_value

    assert _parse_ddm_value("15200", scale=0.001) == 15.2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_olt_polling_service.py::test_parse_temperature_value -v`
Expected: FAIL — `_parse_ddm_value` not found

- [ ] **Step 3: Add `_parse_ddm_value` helper**

In `app/services/network/olt_polling.py`, after the `_parse_distance` function (line ~571):

```python
def _parse_ddm_value(raw: str, *, scale: float = 1.0) -> float | None:
    """Parse a generic DDM numeric value from SNMP (temperature, voltage, bias current).

    Args:
        raw: Raw SNMP value string.
        scale: Multiplier to convert raw integer to real units.

    Returns:
        Parsed float value, or None if unparseable/missing.
    """
    if not raw:
        return None
    lowered = raw.lower().strip()
    if lowered.startswith("no such") or lowered == "":
        return None
    match = re.search(r"(-?\d+)", raw)
    if not match:
        return None
    try:
        raw_int = int(match.group(1))
    except ValueError:
        return None
    if raw_int in _SIGNAL_SENTINELS:
        return None
    return round(raw_int * scale, 4)
```

- [ ] **Step 4: Run parsing tests**

Run: `pytest tests/test_olt_polling_service.py -k "test_parse_temperature or test_parse_voltage or test_parse_bias" -v`
Expected: PASS

- [ ] **Step 5: Add DDM scale constants per vendor**

In `app/services/network/olt_polling.py`, after `_VENDOR_SIGNAL_SCALE` (line ~72):

```python
# DDM value scale factors per vendor.
# Temperature: integer degrees C (scale 1.0) for most vendors.
# Voltage: 0.01V units for Huawei/ZTE, 0.001V for Nokia.
# Bias current: 0.001 mA for Huawei, 0.002 mA for ZTE, 0.001 for Nokia.
_VENDOR_DDM_SCALES: dict[str, dict[str, float]] = {
    "huawei": {"temperature": 1.0, "voltage": 0.01, "bias_current": 0.001},
    "zte": {"temperature": 1.0, "voltage": 0.01, "bias_current": 0.002},
    "nokia": {"temperature": 1.0, "voltage": 0.001, "bias_current": 0.001},
}

_DEFAULT_DDM_SCALES: dict[str, float] = {
    "temperature": 1.0,
    "voltage": 0.01,
    "bias_current": 0.001,
}


def _get_ddm_scales(vendor: str) -> dict[str, float]:
    """Return DDM value scale factors for a vendor."""
    vendor_lower = vendor.lower().strip()
    for key, scales in _VENDOR_DDM_SCALES.items():
        if key in vendor_lower:
            return scales
    return _DEFAULT_DDM_SCALES
```

- [ ] **Step 6: Wire DDM walks into `poll_olt_ont_signals`**

In `app/services/network/olt_polling.py`, inside `poll_olt_ont_signals()`, after the existing `status_raw` walk block (~line 680), add:

```python
    # DDM health telemetry walks (optional — missing OIDs are silently skipped)
    onu_tx_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["onu_tx"], community),
            base_oid=oids["onu_tx"],
        )
        if oids.get("onu_tx")
        else {}
    )
    temperature_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["temperature"], community),
            base_oid=oids["temperature"],
        )
        if oids.get("temperature")
        else {}
    )
    voltage_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["voltage"], community),
            base_oid=oids["voltage"],
        )
        if oids.get("voltage")
        else {}
    )
    bias_current_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["bias_current"], community),
            base_oid=oids["bias_current"],
        )
        if oids.get("bias_current")
        else {}
    )
    offline_reason_snmp_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["offline_reason"], community),
            base_oid=oids["offline_reason"],
        )
        if oids.get("offline_reason")
        else {}
    )
    serial_number_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids["serial_number"], community),
            base_oid=oids["serial_number"],
        )
        if oids.get("serial_number")
        else {}
    )
```

Then update the reading construction loop to include DDM fields. Replace the existing `readings.append(...)` block:

```python
    ddm_scales = _get_ddm_scales(vendor)
    for idx in all_indexes:
        readings.append(
            OntSignalReading(
                onu_index=idx,
                olt_rx_dbm=_parse_signal_value(
                    olt_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="olt_rx",
                    stats=parse_stats,
                ),
                onu_rx_dbm=_parse_signal_value(
                    onu_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_rx",
                    stats=parse_stats,
                ),
                onu_tx_dbm=_parse_signal_value(
                    onu_tx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_tx",
                    stats=parse_stats,
                ),
                distance_m=_parse_distance(distance_raw.get(idx, "")),
                is_online=_parse_online_status(status_raw.get(idx, "")),
                temperature_c=_parse_ddm_value(
                    temperature_raw.get(idx, ""),
                    scale=ddm_scales.get("temperature", 1.0),
                ),
                voltage_v=_parse_ddm_value(
                    voltage_raw.get(idx, ""),
                    scale=ddm_scales.get("voltage", 0.01),
                ),
                bias_current_ma=_parse_ddm_value(
                    bias_current_raw.get(idx, ""),
                    scale=ddm_scales.get("bias_current", 0.001),
                ),
                offline_reason_raw=offline_reason_snmp_raw.get(idx),
                serial_number_raw=serial_number_raw.get(idx),
            )
        )
```

- [ ] **Step 7: Run all polling tests**

Run: `pytest tests/test_olt_polling_service.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/network/olt_polling.py tests/test_olt_polling_service.py
git commit -m "feat: wire DDM health walks into poll_olt_ont_signals"
```

---

### Task 6: Persist DDM readings and offline reason to OntUnit

**Files:**
- Modify: `app/services/network/olt_polling.py` (the update block inside `poll_olt_ont_signals`, ~lines 758-795)
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py

def test_ddm_values_included_in_update_values() -> None:
    """Reading with DDM data should produce update_values with DDM keys."""
    from app.services.network.olt_polling import OntSignalReading

    reading = OntSignalReading(
        onu_index="0.1.3.5",
        olt_rx_dbm=-19.5,
        onu_rx_dbm=-21.0,
        onu_tx_dbm=2.5,
        distance_m=1200,
        is_online=True,
        temperature_c=42.0,
        voltage_v=3.3,
        bias_current_ma=15.2,
        offline_reason_raw=None,
        serial_number_raw=None,
    )
    # Build update_values dict the same way the polling loop does
    update_values: dict = {}
    if reading.olt_rx_dbm is not None:
        update_values["olt_rx_signal_dbm"] = reading.olt_rx_dbm
    if reading.onu_rx_dbm is not None:
        update_values["onu_rx_signal_dbm"] = reading.onu_rx_dbm
    if reading.onu_tx_dbm is not None:
        update_values["onu_tx_signal_dbm"] = reading.onu_tx_dbm
    if reading.temperature_c is not None:
        update_values["ont_temperature_c"] = reading.temperature_c
    if reading.voltage_v is not None:
        update_values["ont_voltage_v"] = reading.voltage_v
    if reading.bias_current_ma is not None:
        update_values["ont_bias_current_ma"] = reading.bias_current_ma

    assert update_values["onu_tx_signal_dbm"] == 2.5
    assert update_values["ont_temperature_c"] == 42.0
    assert update_values["ont_voltage_v"] == 3.3
    assert update_values["ont_bias_current_ma"] == 15.2
```

- [ ] **Step 2: Run test to verify it passes (this is a logic test, no code needed)**

Run: `pytest tests/test_olt_polling_service.py::test_ddm_values_included_in_update_values -v`
Expected: PASS (validates the dict-building pattern before we apply it)

- [ ] **Step 3: Update the OntUnit update block in `poll_olt_ont_signals`**

In `app/services/network/olt_polling.py`, inside the `for ont, reading in targets:` loop, after the existing `distance_meters` line (~line 766), add:

```python
            if reading.onu_tx_dbm is not None:
                update_values["onu_tx_signal_dbm"] = reading.onu_tx_dbm
            if reading.temperature_c is not None:
                update_values["ont_temperature_c"] = reading.temperature_c
            if reading.voltage_v is not None:
                update_values["ont_voltage_v"] = reading.voltage_v
            if reading.bias_current_ma is not None:
                update_values["ont_bias_current_ma"] = reading.bias_current_ma
```

Also, after the online/offline status block, add SNMP-sourced offline reason override (~after line 785):

```python
            # Use SNMP offline_reason OID if available (more precise than status code)
            if (
                reading.offline_reason_raw
                and reading.is_online is not None
                and not reading.is_online
            ):
                snmp_reason = _derive_offline_reason(reading.offline_reason_raw)
                if snmp_reason:
                    try:
                        update_values["offline_reason"] = OnuOfflineReason(snmp_reason)
                    except ValueError:
                        pass
```

- [ ] **Step 4: Run all polling tests**

Run: `pytest tests/test_olt_polling_service.py -v`
Expected: ALL PASS

- [ ] **Step 5: Lint and type-check**

```bash
ruff check app/services/network/olt_polling.py
mypy app/services/network/olt_polling.py --ignore-missing-imports
```

- [ ] **Step 6: Commit**

```bash
git add app/services/network/olt_polling.py tests/test_olt_polling_service.py
git commit -m "feat: persist DDM health values and SNMP offline reason to OntUnit"
```

---

### Task 7: Add `ont.ddm_alert` event type and threshold alerting

**Files:**
- Modify: `app/services/events/types.py:85-94`
- Modify: `app/services/network/olt_polling.py` (alert logic block, ~lines 820-894)
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py

def test_event_type_ont_ddm_alert_exists() -> None:
    from app.services.events.types import EventType

    assert hasattr(EventType, "ont_ddm_alert")
    assert EventType.ont_ddm_alert.value == "ont.ddm_alert"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_olt_polling_service.py::test_event_type_ont_ddm_alert_exists -v`
Expected: FAIL — `EventType` has no attribute `ont_ddm_alert`

- [ ] **Step 3: Add event type**

In `app/services/events/types.py`, after `ont_feature_toggled` (line 94):

```python
    ont_ddm_alert = "ont.ddm_alert"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_olt_polling_service.py::test_event_type_ont_ddm_alert_exists -v`
Expected: PASS

- [ ] **Step 5: Add DDM threshold constants and alert logic**

In `app/services/network/olt_polling.py`, after `_DEFAULT_ALERT_COOLDOWN_MINUTES` (~line 155):

```python
# DDM health thresholds — alert when exceeded
_DDM_TEMPERATURE_WARN_C = 65.0
_DDM_TEMPERATURE_CRIT_C = 75.0
_DDM_VOLTAGE_LOW_V = 3.0
_DDM_VOLTAGE_HIGH_V = 3.6
_DDM_BIAS_CURRENT_WARN_MA = 60.0
```

Then in the `for ont, reading in targets:` loop, after the signal delta detection block (~line 894), add:

```python
            # DDM health alerts — temperature, voltage, bias current
            if reading.temperature_c is not None and reading.temperature_c > _DDM_TEMPERATURE_WARN_C:
                severity = "critical" if reading.temperature_c > _DDM_TEMPERATURE_CRIT_C else "warning"
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "temperature",
                            "value": reading.temperature_c,
                            "unit": "C",
                            "severity": severity,
                        },
                    )
                )
            if reading.voltage_v is not None and (
                reading.voltage_v < _DDM_VOLTAGE_LOW_V or reading.voltage_v > _DDM_VOLTAGE_HIGH_V
            ):
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "voltage",
                            "value": reading.voltage_v,
                            "unit": "V",
                            "severity": "warning",
                        },
                    )
                )
            if reading.bias_current_ma is not None and reading.bias_current_ma > _DDM_BIAS_CURRENT_WARN_MA:
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "bias_current",
                            "value": reading.bias_current_ma,
                            "unit": "mA",
                            "severity": "warning",
                        },
                    )
                )
```

Then in the event emission block (~line 914), add the `ddm_alert` case:

```python
            elif transition == "ddm_alert":
                emit_event(db, EventType.ont_ddm_alert, payload, actor="system")
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_olt_polling_service.py -v`
Expected: ALL PASS

- [ ] **Step 7: Lint and type-check**

```bash
ruff check app/services/network/olt_polling.py app/services/events/types.py
mypy app/services/network/olt_polling.py --ignore-missing-imports
```

- [ ] **Step 8: Commit**

```bash
git add app/services/network/olt_polling.py app/services/events/types.py tests/test_olt_polling_service.py
git commit -m "feat: add ont.ddm_alert event and temperature/voltage/bias alerting"
```

---

### Task 8: Push DDM metrics to VictoriaMetrics

**Files:**
- Modify: `app/services/network/olt_polling.py` (function `_push_signal_metrics`, ~lines 1145-1267)
- Modify: `tests/test_olt_polling_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_olt_polling_service.py
from unittest.mock import patch, MagicMock

def test_push_signal_metrics_includes_ddm_lines(test_db) -> None:
    """VictoriaMetrics push should include DDM metric lines when data is present."""
    from app.models.network import OntUnit, OnuOnlineStatus
    from datetime import UTC, datetime
    import uuid

    # Create a test ONT with DDM data
    ont = OntUnit(
        id=uuid.uuid4(),
        serial_number="HWTCDDM00001",
        olt_rx_signal_dbm=-19.5,
        onu_rx_signal_dbm=-21.0,
        onu_tx_signal_dbm=2.5,
        ont_temperature_c=42.0,
        ont_voltage_v=3.3,
        ont_bias_current_ma=15.2,
        online_status=OnuOnlineStatus.online,
        signal_updated_at=datetime.now(UTC),
        is_active=True,
    )
    test_db.add(ont)
    test_db.flush()

    with patch("app.services.network.olt_polling.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()

        from app.services.network.olt_polling import push_signal_metrics_to_victoriametrics
        count = push_signal_metrics_to_victoriametrics(test_db)

    # Verify DDM metric lines were included
    call_args = mock_client.post.call_args
    content = call_args.kwargs.get("content", "") if call_args else ""
    assert "ont_onu_tx_dbm" in content
    assert "ont_temperature_c" in content
    assert "ont_voltage_v" in content
    assert "ont_bias_current_ma" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_olt_polling_service.py::test_push_signal_metrics_includes_ddm_lines -v`
Expected: FAIL — `ont_onu_tx_dbm` not in content

- [ ] **Step 3: Extend `_push_signal_metrics` to include DDM metrics**

In `app/services/network/olt_polling.py`, update the query in `_push_signal_metrics` to include new columns. In the `select()` statement (~line 1156), add:

```python
            OntUnit.onu_tx_signal_dbm,
            OntUnit.ont_temperature_c,
            OntUnit.ont_voltage_v,
            OntUnit.ont_bias_current_ma,
```

Then in the metric line generation loop (~after line 1206), add:

```python
        if row.onu_tx_signal_dbm is not None:
            lines.append(f"ont_onu_tx_dbm{{{labels}}} {row.onu_tx_signal_dbm} {now_ms}")
        if row.ont_temperature_c is not None:
            lines.append(f"ont_temperature_c{{{labels}}} {row.ont_temperature_c} {now_ms}")
        if row.ont_voltage_v is not None:
            lines.append(f"ont_voltage_v{{{labels}}} {row.ont_voltage_v} {now_ms}")
        if row.ont_bias_current_ma is not None:
            lines.append(f"ont_bias_current_ma{{{labels}}} {row.ont_bias_current_ma} {now_ms}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_olt_polling_service.py::test_push_signal_metrics_includes_ddm_lines -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

```bash
ruff check app/services/network/olt_polling.py
mypy app/services/network/olt_polling.py --ignore-missing-imports
```

- [ ] **Step 6: Commit**

```bash
git add app/services/network/olt_polling.py tests/test_olt_polling_service.py
git commit -m "feat: push DDM health metrics to VictoriaMetrics"
```

---

### Task 9: Final verification

**Files:**
- All modified files

- [ ] **Step 1: Run full quality checks**

```bash
make check
```
Expected: All lint, type-check, and security scans pass.

- [ ] **Step 2: Run full test suite**

```bash
make test
```
Expected: All tests pass.

- [ ] **Step 3: Verify migration applies cleanly**

```bash
make migrate-down && make migrate
```
Expected: Migration rolls back and re-applies without error.

- [ ] **Step 4: Final commit if any formatting fixes were needed**

```bash
git add -A
git status
# Only commit if there are changes from auto-formatting
git commit -m "chore: formatting fixes from make check"
```
