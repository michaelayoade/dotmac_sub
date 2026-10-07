"""Real FreeRADIUS probe against a disposable, schema-owned test_radius DB.

No application/worker is running. This is external projection acceptance,
not a substitute for migrated application PostgreSQL integration tests.
"""

from __future__ import annotations

import re
import subprocess
import time


def sql(statement: str) -> str:
    return subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-At", "-c", statement],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


def authenticate() -> str:
    request = (
        'User-Name = "test-login"\nUser-Password = "fixture-password"\n'
        "NAS-IP-Address = 127.0.0.1\nMessage-Authenticator = 0x00000000000000000000000000000000\n"
    )
    result = subprocess.run(
        [
            "radclient",
            "-x",
            "-r",
            "1",
            "-t",
            "2",
            "127.0.0.1:1812",
            "auth",
            "testing123",
        ],
        input=request,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout + result.stderr


def main() -> int:
    sql("""
        INSERT INTO radcheck (username,attribute,op,value) VALUES
          ('test-login','Auth-Type',':=','Reject'),
          ('test-login','Dotmac-Test-Cleartext-Password',':=','fixture-password'),
          ('test-login','Dotmac-Test-Until',':=',floor(extract(epoch from now()) + 150)::text);
        INSERT INTO radreply (username,attribute,op,value) VALUES
          ('test-login','Dotmac-Test-Mikrotik-Rate-Limit',':=','100M/100M');
        INSERT INTO radusergroup (username,groupname,priority) VALUES
          ('test-login','Dotmac-Test-full-plan',1);
        INSERT INTO radgroupreply (groupname,attribute,op,value) VALUES
          ('full-plan','Session-Timeout',':=','999999');
    """)
    deadline = time.monotonic() + 20
    while True:
        reply = authenticate()
        if "Received Access-Accept" in reply:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("Test authentication failed: " + reply)
        time.sleep(0.5)
    timeout = re.search(r"Session-Timeout = (\d+)", reply)
    assert timeout is not None and 1 <= int(timeout.group(1)) <= 150, reply
    assert 'Mikrotik-Rate-Limit = "100M/100M"' in reply, reply
    assert "Mikrotik-Address-List" not in reply, reply
    # Make the absolute interval expire, retaining every override row. There
    # is no application timer or dispatcher to clean up this projection.
    sql("UPDATE radcheck SET value='1' WHERE attribute='Dotmac-Test-Until'")
    rejected = authenticate()
    assert "Received Access-Reject" in rejected, rejected
    # The current paid state must now apply; never restore the original reject.
    sql("""
        DELETE FROM radcheck WHERE attribute='Auth-Type';
        INSERT INTO radcheck (username,attribute,op,value) VALUES
          ('test-login','Cleartext-Password',':=','fixture-password');
        INSERT INTO radreply (username,attribute,op,value) VALUES
          ('test-login','Mikrotik-Rate-Limit',':=','100M/100M');
    """)
    paid = authenticate()
    assert "Received Access-Accept" in paid, paid
    assert "Session-Timeout" not in paid, paid
    print(
        "Real RADIUS accepts full test access, caps group timeout, rejects expired debt, and accepts current paid access."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
