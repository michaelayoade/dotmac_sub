"""Exercise the checked-in RADIUS contract in a disposable real server.

The existing PostgreSQL integration CI owner supplies Docker. The application
schema is migration-owned; this separate external RADIUS projection database
is created from config/freeradius/schema.sql, its own authoritative schema.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from scripts.ci import template_database

ROOT = Path(__file__).resolve().parents[2]


def test_real_radius_authentication_and_worker_independent_expiry(engine):
    docker = shutil.which("docker")
    assert docker is not None, (
        "RADIUS integration requires Docker on the development/CI host"
    )
    base = engine.url
    name = "test_connection_radius_" + uuid4().hex
    container = "test-connection-radius-" + uuid4().hex
    maintenance = base.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    with psycopg.connect(maintenance, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    environ = dict(os.environ)
    values = {
        "PGHOST": str(base.host or "127.0.0.1"),
        "PGPORT": str(base.port or 5432),
        "PGUSER": str(base.username),
        "PGPASSWORD": str(base.password),
        "PGDATABASE": name,
        "FREERADIUS_DB_HOST": str(base.host or "127.0.0.1"),
        "FREERADIUS_DB_PORT": str(base.port or 5432),
        "FREERADIUS_DB_USER": str(base.username),
        "FREERADIUS_DB_PASS": str(base.password),
        "FREERADIUS_DB_NAME": name,
    }
    environ.update(values)
    command = [docker, "run", "--rm", "--name", container, "--network", "host"]
    for key in values:
        command.extend(("-e", key))
    command.extend(
        (
            "-v",
            f"{ROOT}:/repo:ro",
            "ubuntu:24.04",
            "bash",
            "-c",
            """
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq freeradius freeradius-postgresql freeradius-utils postgresql-client python3 > /tmp/install.log 2>&1
psql -v ON_ERROR_STOP=1 -f /repo/config/freeradius/schema.sql
psql -v ON_ERROR_STOP=1 -f /repo/config/freeradius/upgrade_002_access_state_groups.sql
cp /repo/config/freeradius/mods-enabled/sql /etc/freeradius/3.0/mods-enabled/sql
cp /repo/config/freeradius/sites-enabled/default /etc/freeradius/3.0/sites-enabled/default
freeradius -XC
freeradius -f > /tmp/radius.log 2>&1 &
radius_pid=$!
trap 'kill "$radius_pid" 2>/dev/null || true' EXIT
python3 /repo/scripts/ci/test_connection_radius_probe.py || { cat /tmp/radius.log; exit 1; }
""",
        )
    )
    try:
        result = subprocess.run(
            command, env=environ, capture_output=True, text=True, timeout=300
        )
        assert result.returncode == 0, result.stdout[-18000:] + result.stderr[-5000:]
    finally:
        subprocess.run(
            [docker, "rm", "-f", container],
            capture_output=True,
            check=False,
            timeout=15,
        )
        template_database.drop_database(base, name)
