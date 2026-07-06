"""Splice inference (outage classifier P3, design §6).

Co-failure clustering + correlated-Rx droop recover the unpollable sub-PON
splitter branches from the ont_signal_observations time series, and reconcile
the inferred grouping against the plant records (diff-not-mirror).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network import (
    OLTDevice,
    OntSignalObservation,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    Splitter,
    SplitterPort,
)
from app.services.topology.splice_inference import (
    detect_rx_droop,
    infer_branches,
    reconcile_with_records,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=365)


def _pon(db):
    olt = OLTDevice(name="OLT-1", hostname="o1", mgmt_ip="10.0.0.5")
    db.add(olt)
    db.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/0")
    db.add(pon)
    db.flush()
    return olt, pon


def _ont(db, olt, pon, serial, splitter_port_id=None):
    ont = OntUnit(
        serial_number=serial,
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        splitter_port_id=splitter_port_id,
    )
    db.add(ont)
    db.flush()
    return ont


def _obs(db, ont, pon, *, status, rx, at):
    db.add(
        OntSignalObservation(
            ont_unit_id=ont.id,
            olt_device_id=ont.olt_device_id,
            pon_port_id=pon.id,
            olt_status=status,
            rx_signal_dbm=rx,
            observed_at=at,
        )
    )


def _episode(db, pon, *, offline, online, at):
    for ont in offline:
        _obs(db, ont, pon, status=OnuOnlineStatus.offline, rx=None, at=at)
    for ont in online:
        _obs(db, ont, pon, status=OnuOnlineStatus.online, rx=-20.0, at=at)


def _ids(branch):
    return set(branch["ont_unit_ids"])


# --- co-failure clustering -------------------------------------------------


def test_cofailing_onts_cluster_independent_one_does_not(db_session):
    olt, pon = _pon(db_session)
    a = _ont(db_session, olt, pon, "A")
    b = _ont(db_session, olt, pon, "B")
    c = _ont(db_session, olt, pon, "C")
    d = _ont(db_session, olt, pon, "D")

    # 3 episodes where A+B go dark together while C+D survive (partial PON).
    for k in range(3):
        _episode(
            db_session,
            pon,
            offline=[a, b],
            online=[c, d],
            at=NOW - timedelta(hours=k),
        )
    # One episode where C alone blips — no co-failure partner.
    _episode(
        db_session, pon, offline=[c], online=[a, b, d], at=NOW - timedelta(hours=5)
    )
    db_session.flush()

    branches = infer_branches(db_session, pon.id, window=WINDOW, now=NOW)

    assert len(branches) == 1
    assert _ids(branches[0]) == {a.id, b.id}
    assert branches[0]["support"] == 3
    assert branches[0]["confidence"] == "medium"


def test_whole_pon_outage_is_not_a_branch(db_session):
    olt, pon = _pon(db_session)
    a = _ont(db_session, olt, pon, "A")
    b = _ont(db_session, olt, pon, "B")

    # Every ONT dark every episode => feeder/OLT outage, not a shared branch.
    for k in range(4):
        _episode(
            db_session, pon, offline=[a, b], online=[], at=NOW - timedelta(hours=k)
        )
    db_session.flush()

    assert infer_branches(db_session, pon.id, window=WINDOW, now=NOW) == []


# --- correlated Rx droop ---------------------------------------------------


def test_correlated_equal_db_droop_flagged_noise_ignored(db_session):
    olt, pon = _pon(db_session)
    a = _ont(db_session, olt, pon, "A")
    b = _ont(db_session, olt, pon, "B")
    c = _ont(db_session, olt, pon, "C")
    noise = _ont(db_session, olt, pon, "N")
    lone = _ont(db_session, olt, pon, "L")

    start = NOW - timedelta(days=2)
    # A,B,C each lose ~3 dB (a shared upstream splice attenuating the branch).
    for ont, base in ((a, -18.0), (b, -19.0), (c, -20.0)):
        _obs(db_session, ont, pon, status=OnuOnlineStatus.online, rx=base, at=start)
        _obs(db_session, ont, pon, status=OnuOnlineStatus.online, rx=base - 3.0, at=NOW)
    # Noise ONT barely moves (< threshold) -> excluded.
    _obs(db_session, noise, pon, status=OnuOnlineStatus.online, rx=-18.0, at=start)
    _obs(db_session, noise, pon, status=OnuOnlineStatus.online, rx=-18.2, at=NOW)
    # Lone ONT droops a very different amount -> not correlated, and alone.
    _obs(db_session, lone, pon, status=OnuOnlineStatus.online, rx=-18.0, at=start)
    _obs(db_session, lone, pon, status=OnuOnlineStatus.online, rx=-25.0, at=NOW)
    db_session.flush()

    droops = detect_rx_droop(db_session, pon.id, window=WINDOW, now=NOW)

    assert len(droops) == 1
    assert _ids(droops[0]) == {a.id, b.id, c.id}
    assert droops[0]["shared_shift_db"] == -3.0
    assert droops[0]["confidence"] == "high"


# --- reconcile inference vs plant records ----------------------------------


def test_reconcile_reports_record_reality_disagreement(db_session):
    olt, pon = _pon(db_session)
    splitter = Splitter(name="SPL-A")
    db_session.add(splitter)
    db_session.flush()
    ports = [SplitterPort(splitter_id=splitter.id, port_number=n) for n in range(1, 5)]
    db_session.add_all(ports)
    db_session.flush()

    # A,B co-fail (telemetry branch) but records put them on DIFFERENT ports.
    a = _ont(db_session, olt, pon, "A", splitter_port_id=ports[0].id)
    b = _ont(db_session, olt, pon, "B", splitter_port_id=ports[1].id)
    # E,F co-fail AND records agree (same port) -> agrees.
    e = _ont(db_session, olt, pon, "E", splitter_port_id=ports[2].id)
    f = _ont(db_session, olt, pon, "F", splitter_port_id=ports[2].id)
    # C,D share a record port but NEVER co-fail -> records claim a branch the
    # telemetry can't confirm.
    c = _ont(db_session, olt, pon, "C", splitter_port_id=ports[3].id)
    d = _ont(db_session, olt, pon, "D", splitter_port_id=ports[3].id)

    for k in range(3):
        _episode(
            db_session,
            pon,
            offline=[a, b],
            online=[c, d, e, f],
            at=NOW - timedelta(hours=k),
        )
        _episode(
            db_session,
            pon,
            offline=[e, f],
            online=[a, b, c, d],
            at=NOW - timedelta(hours=k, minutes=30),
        )
    db_session.flush()

    out = reconcile_with_records(db_session, pon.id, window=WINDOW, now=NOW)

    assert [set(x["ont_unit_ids"]) for x in out["agrees"]] == [{e.id, f.id}]
    assert [set(x["ont_unit_ids"]) for x in out["missing_in_records"]] == [{a.id, b.id}]
    reality = out["missing_in_reality"]
    assert len(reality) == 1
    assert set(reality[0]["ont_unit_ids"]) == {c.id, d.id}
    assert reality[0]["splitter_port_id"] == ports[3].id
