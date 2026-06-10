"""Tests for the radacct importer's ghost-session handling.

FreeRADIUS UPDATEs radacct rows in place on Interim-Update/Stop (same
radacctid), so the importer's forward-only cursor alone would never see a
session again after first ingesting it — its Stop would be lost and the
session would render "active" forever. These tests cover the open-session
refresh pass, the persisted last_update_at, the stale-session reaper, and the
no-flap guard between the two.

The fake radacct lives in a throwaway SQLite file; the importer's SQL is
dialect-neutral on purpose.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text

from app.models.catalog import AccessCredential, SubscriptionStatus
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import usage as usage_service

_RADACCT_DDL = """
CREATE TABLE radacct (
    radacctid INTEGER PRIMARY KEY,
    acctsessionid TEXT,
    username TEXT,
    nasipaddress TEXT,
    acctstarttime TIMESTAMP,
    acctupdatetime TIMESTAMP,
    acctstoptime TIMESTAMP,
    acctinputoctets BIGINT,
    acctoutputoctets BIGINT,
    acctterminatecause TEXT,
    callingstationid TEXT,
    framedipaddress TEXT,
    framedipv6prefix TEXT,
    delegatedipv6prefix TEXT,
    nasportid TEXT,
    calledstationid TEXT
)
"""

# Older deployments lack the framed-address columns entirely; the importer
# probes and must cope.
_RADACCT_DDL_LEGACY = """
CREATE TABLE radacct (
    radacctid INTEGER PRIMARY KEY,
    acctsessionid TEXT,
    username TEXT,
    nasipaddress TEXT,
    acctstarttime TIMESTAMP,
    acctupdatetime TIMESTAMP,
    acctstoptime TIMESTAMP,
    acctinputoctets BIGINT,
    acctoutputoctets BIGINT,
    acctterminatecause TEXT,
    callingstationid TEXT
)
"""


def _naive(dt: datetime) -> datetime:
    """Local rows read back from the SQLite test DB are tz-naive UTC."""
    return dt.astimezone(UTC).replace(tzinfo=None)


def _make_radacct(tmp_path, monkeypatch, *, ddl=_RADACCT_DDL):
    url = f"sqlite:///{tmp_path / 'radacct.sqlite'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(ddl))
    monkeypatch.setattr(usage_service, "_radius_accounting_db_url", lambda: url)
    # No Redis in unit tests — keeps the bandwidth-delta emit a no-op.
    monkeypatch.delenv("REDIS_URL", raising=False)
    return engine


def _insert_radacct_row(engine, **values):
    cols = ", ".join(values)
    params = ", ".join(f":{c}" for c in values)
    with engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO radacct ({cols}) VALUES ({params})"),  # noqa: S608
            values,
        )


def _update_radacct_row(engine, radacctid, **values):
    assignments = ", ".join(f"{c} = :{c}" for c in values)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE radacct SET {assignments} WHERE radacctid = :rid"),  # noqa: S608
            {**values, "rid": radacctid},
        )


def _credential(db_session, subscription, username="40001001"):
    credential = AccessCredential(
        subscriber_id=subscription.subscriber_id,
        username=username,
        secret_hash="hashed-secret",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    db_session.refresh(credential)
    return credential


def _local_session(db_session, credential):
    return (
        db_session.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.access_credential_id == credential.id)
        .one()
    )


def test_import_persists_last_update_at(
    db_session, subscription, tmp_path, monkeypatch
):
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    start = datetime.now(UTC) - timedelta(hours=2)
    update = datetime.now(UTC) - timedelta(minutes=5)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-1",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=update.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
    )

    result = usage_service.import_radius_accounting(db_session)

    assert result["ok"] is True
    assert result["processed"] == 1
    local = _local_session(db_session, credential)
    assert local.session_end is None
    assert local.last_update_at == _naive(update)
    assert local.status_type == AccountingStatus.interim


def test_refresh_window_round_robins_instead_of_starving(
    db_session, subscription, tmp_path, monkeypatch
):
    """An unchanging ghost must not pin the refresh window: with batch=1, the
    second pass must attempt the session the first pass skipped."""
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    ghost_start = datetime.now(UTC) - timedelta(days=30)
    live_update = datetime.now(UTC) - timedelta(minutes=3)
    # Stalest session: a ghost whose radacct row never changes.
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="ghost-1",
        username=credential.username,
        acctstarttime=ghost_start.isoformat(),
        acctupdatetime=ghost_start.isoformat(),
        acctinputoctets=10,
        acctoutputoctets=10,
    )
    # Fresher session that would starve under stalest-first ordering.
    _insert_radacct_row(
        engine,
        radacctid=2,
        acctsessionid="live-1",
        username=credential.username,
        acctstarttime=live_update.isoformat(),
        acctupdatetime=live_update.isoformat(),
        acctinputoctets=20,
        acctoutputoctets=20,
    )
    usage_service.import_radius_accounting(db_session)  # ingest both via cursor

    monkeypatch.setattr(usage_service, "_RADIUS_REFRESH_BATCH", 1)
    usage_service.import_radius_accounting(db_session)  # refresh attempts ghost
    usage_service.import_radius_accounting(db_session)  # must attempt live-1

    sessions = {
        s.session_id: s
        for s in db_session.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.access_credential_id == credential.id)
        .all()
    }
    assert sessions["ghost-1"].refresh_attempted_at is not None
    assert sessions["live-1"].refresh_attempted_at is not None


def test_refresh_pass_picks_up_in_place_stop(
    db_session, subscription, tmp_path, monkeypatch
):
    """A Stop written into an already-ingested radacct row (same radacctid,
    behind the cursor) must still close the local session."""
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    start = datetime.now(UTC) - timedelta(hours=2)
    update = datetime.now(UTC) - timedelta(hours=1)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-1",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=update.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
    )
    usage_service.import_radius_accounting(db_session)
    assert _local_session(db_session, credential).session_end is None

    # FreeRADIUS logs the Stop by updating the row in place; the cursor (now
    # past radacctid 1) never sees it again.
    stop = datetime.now(UTC) - timedelta(minutes=2)
    _update_radacct_row(
        engine,
        1,
        acctupdatetime=stop.isoformat(),
        acctstoptime=stop.isoformat(),
        acctinputoctets=5000,
        acctoutputoctets=9000,
        acctterminatecause="User-Request",
    )

    result = usage_service.import_radius_accounting(db_session)

    assert result["processed"] == 0  # nothing new past the cursor
    assert result["refreshed"] == 1
    local = _local_session(db_session, credential)
    assert local.session_end == _naive(stop)
    assert local.last_update_at == _naive(stop)
    assert local.status_type == AccountingStatus.stop
    assert local.terminate_cause == "User-Request"
    assert local.input_octets == 5000


def _activate(db_session, subscription):
    """The conftest subscription defaults to pending; the importer only links
    sessions (and so only writes back IPs) to an *active* subscription."""
    subscription.status = SubscriptionStatus.active
    db_session.commit()


def test_import_persists_framed_ips_and_writes_back_subscription(
    db_session, subscription, tmp_path, monkeypatch
):
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    _activate(db_session, subscription)
    start = datetime.now(UTC) - timedelta(minutes=30)
    update = datetime.now(UTC) - timedelta(minutes=2)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-1",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=update.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
        # psycopg hands inet host addresses back with their /32 — must strip.
        framedipaddress="100.64.10.20/32",
        delegatedipv6prefix="2a02:db8:100::/56",
        nasportid="pppoe-user1",
        calledstationid="service1",
    )

    usage_service.import_radius_accounting(db_session)

    local = _local_session(db_session, credential)
    assert local.framed_ip_address == "100.64.10.20"
    assert local.delegated_ipv6_prefix == "2a02:db8:100::/56"
    assert local.framed_ipv6_prefix is None
    assert local.nas_port_id == "pppoe-user1"
    assert local.called_station_id == "service1"
    # Live session → the subscription's current address follows it.
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "100.64.10.20"
    assert subscription.ipv6_address == "2a02:db8:100::/56"


def test_import_survives_radacct_without_framed_ip_columns(
    db_session, subscription, tmp_path, monkeypatch
):
    engine = _make_radacct(tmp_path, monkeypatch, ddl=_RADACCT_DDL_LEGACY)
    credential = _credential(db_session, subscription)
    start = datetime.now(UTC) - timedelta(minutes=30)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-1",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=start.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
    )

    result = usage_service.import_radius_accounting(db_session)

    assert result["ok"] is True
    assert result["processed"] == 1
    local = _local_session(db_session, credential)
    assert local.framed_ip_address is None
    db_session.refresh(subscription)
    assert subscription.ipv4_address is None


def test_stop_row_does_not_write_back_subscription_ip(
    db_session, subscription, tmp_path, monkeypatch
):
    """A historical Stop must not overwrite the subscriber's current address."""
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    subscription.ipv4_address = "100.64.99.99"
    _activate(db_session, subscription)
    start = datetime.now(UTC) - timedelta(hours=5)
    stop = datetime.now(UTC) - timedelta(hours=4)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-old",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=stop.isoformat(),
        acctstoptime=stop.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
        acctterminatecause="User-Request",
        framedipaddress="100.64.10.20",
    )

    usage_service.import_radius_accounting(db_session)

    local = _local_session(db_session, credential)
    # The session itself still records the address it used...
    assert local.framed_ip_address == "100.64.10.20"
    # ...but the subscription's current address is untouched.
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "100.64.99.99"


def test_find_by_ip_reverse_lookup(db_session, subscription):
    """Abuse-desk question: who held 100.64.10.20 at time T?"""
    credential = _credential(db_session, subscription)
    now = datetime.now(UTC)
    old = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="held-earlier",
        status_type=AccountingStatus.stop,
        session_start=now - timedelta(hours=10),
        session_end=now - timedelta(hours=8),
        framed_ip_address="100.64.10.20",
    )
    current = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="holds-now",
        status_type=AccountingStatus.interim,
        session_start=now - timedelta(hours=2),
        framed_ip_address="100.64.10.20",
    )
    other = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="different-ip",
        status_type=AccountingStatus.interim,
        session_start=now - timedelta(hours=2),
        framed_ip_address="100.64.10.21",
    )
    db_session.add_all([old, current, other])
    db_session.commit()

    sessions = usage_service.radius_accounting_sessions.find_by_ip
    all_holders = sessions(db_session, "100.64.10.20")
    assert {s.session_id for s in all_holders} == {"held-earlier", "holds-now"}

    at_nine_hours_ago = sessions(
        db_session, "100.64.10.20", at=now - timedelta(hours=9)
    )
    assert [s.session_id for s in at_nine_hours_ago] == ["held-earlier"]

    live_now = sessions(db_session, "100.64.10.20", at=now)
    assert [s.session_id for s in live_now] == ["holds-now"]


def test_reaper_closes_stale_spares_fresh(db_session, subscription):
    credential = _credential(db_session, subscription)
    now = datetime.now(UTC)
    stale_seen = now - timedelta(hours=3)
    ghost = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="ghost-1",
        status_type=AccountingStatus.interim,
        session_start=now - timedelta(days=2),
        last_update_at=stale_seen,
    )
    live = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="live-1",
        status_type=AccountingStatus.interim,
        session_start=now - timedelta(days=2),
        last_update_at=now - timedelta(minutes=1),
    )
    db_session.add_all([ghost, live])
    db_session.commit()

    result = usage_service.reap_stale_radius_sessions(
        db_session, stale_after_seconds=3600
    )

    assert result["reaped"] == 1
    db_session.refresh(ghost)
    db_session.refresh(live)
    # Synthetic end = last time the session was actually seen, not reap time.
    assert ghost.session_end == _naive(stale_seen)
    assert ghost.terminate_cause == "reaped"
    assert ghost.status_type == AccountingStatus.stop
    assert live.session_end is None
    assert live.terminate_cause is None


def test_reaper_falls_back_to_session_start(db_session, subscription):
    """Pre-backfill rows (last_update_at NULL) still reap via session_start."""
    credential = _credential(db_session, subscription)
    started = datetime.now(UTC) - timedelta(days=30)
    ghost = RadiusAccountingSession(
        access_credential_id=credential.id,
        subscription_id=subscription.id,
        session_id="ghost-legacy",
        status_type=AccountingStatus.start,
        session_start=started,
    )
    db_session.add(ghost)
    db_session.commit()

    result = usage_service.reap_stale_radius_sessions(
        db_session, stale_after_seconds=3600
    )

    assert result["reaped"] == 1
    db_session.refresh(ghost)
    assert ghost.session_end == _naive(started)
    assert ghost.terminate_cause == "reaped"


def test_reaped_session_does_not_flap_but_revives_on_new_data(
    db_session, subscription, tmp_path, monkeypatch
):
    """The refresh pass re-reads recently reaped sessions. An unchanged radacct
    row must not reopen one (reap → refresh → reap flap); genuinely new
    accounting data must."""
    engine = _make_radacct(tmp_path, monkeypatch)
    credential = _credential(db_session, subscription)
    start = datetime.now(UTC) - timedelta(hours=4)
    update = datetime.now(UTC) - timedelta(hours=2)
    _insert_radacct_row(
        engine,
        radacctid=1,
        acctsessionid="sess-1",
        username=credential.username,
        acctstarttime=start.isoformat(),
        acctupdatetime=update.isoformat(),
        acctinputoctets=1000,
        acctoutputoctets=2000,
    )
    usage_service.import_radius_accounting(db_session)
    usage_service.reap_stale_radius_sessions(db_session, stale_after_seconds=3600)
    local = _local_session(db_session, credential)
    assert local.terminate_cause == "reaped"

    # radacct unchanged → refresh must keep the reaped close.
    result = usage_service.import_radius_accounting(db_session)
    assert result["refreshed"] == 0
    db_session.refresh(local)
    assert local.session_end is not None
    assert local.terminate_cause == "reaped"

    # The session turns out to be alive: a fresher interim update arrives.
    revived_at = datetime.now(UTC) - timedelta(minutes=1)
    _update_radacct_row(
        engine,
        1,
        acctupdatetime=revived_at.isoformat(),
        acctinputoctets=8000,
        acctoutputoctets=16000,
    )
    result = usage_service.import_radius_accounting(db_session)
    assert result["refreshed"] == 1
    db_session.refresh(local)
    assert local.session_end is None
    assert local.terminate_cause is None
    assert local.last_update_at == _naive(revived_at)
