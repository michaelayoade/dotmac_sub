"""Unit coverage for the reusable radcheck/radreply write primitive.

`radius_population.populate()` is never driven end-to-end in the suite (its
psycopg write path is Seabone-validated). This pins the extracted single write
primitive — `_write_radius_projection` — against a mock cursor so the DELETE +
INSERT contract is covered here and any regression in the SQL, ordering, or
reject/password split fails fast. Same contract the full sweep relied on inline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from app.services.radius_population import _write_radius_projection


def _work(login, cleartext, attrs, mode):
    # (login, cleartext, attrs, blocked_flag, status, mode) — the projection tuple
    return (login, cleartext, attrs, mode != "active", None, mode)


def test_active_user_writes_password_and_radreply():
    cur = MagicMock()
    work = [_work("alice", "pw-a", [("Framed-IP-Address", ":=", "10.0.0.5")], "active")]

    _write_radius_projection(cur, work, ["alice"])

    cur.execute.assert_any_call(
        "DELETE FROM radcheck WHERE username = ANY(%s)", (["alice"],)
    )
    cur.execute.assert_any_call(
        "DELETE FROM radreply WHERE username = ANY(%s)", (["alice"],)
    )
    cur.executemany.assert_any_call(
        "INSERT INTO radcheck (username, attribute, op, value) "
        "VALUES (%s, 'Cleartext-Password', ':=', %s)",
        [("alice", "pw-a")],
    )
    cur.executemany.assert_any_call(
        "INSERT INTO radreply (username, attribute, op, value) VALUES (%s, %s, %s, %s)",
        [("alice", "Framed-IP-Address", ":=", "10.0.0.5")],
    )


def test_reject_user_gets_reject_row_and_no_radreply():
    cur = MagicMock()
    # a hard-rejected user carries attrs but they must NOT be written as radreply
    work = [_work("bob", "pw-b", [("Framed-IP-Address", ":=", "10.0.0.9")], "reject")]

    _write_radius_projection(cur, work, ["bob"])

    cur.executemany.assert_any_call(
        "INSERT INTO radcheck (username, attribute, op, value) "
        "VALUES (%s, 'Auth-Type', ':=', 'Reject')",
        [("bob",)],
    )
    # no Cleartext-Password and no radreply rows for a rejected user
    for c in cur.executemany.call_args_list:
        assert "Cleartext-Password" not in c.args[0]
        if c.args[0].startswith("INSERT INTO radreply"):
            assert c.args[1] == []


def test_delete_set_is_explicit_so_removed_users_are_purged():
    """A username in delete_usernames but absent from work is deleted, not
    reinserted — the removal path a scoped reconcile depends on."""
    cur = MagicMock()
    work = [_work("alice", "pw-a", [], "active")]

    _write_radius_projection(cur, work, ["alice", "gone"])

    # both usernames deleted from radcheck and radreply
    assert (
        call("DELETE FROM radcheck WHERE username = ANY(%s)", (["alice", "gone"],))
        in cur.execute.call_args_list
    )
    assert (
        call("DELETE FROM radreply WHERE username = ANY(%s)", (["alice", "gone"],))
        in cur.execute.call_args_list
    )
    # only the present user gets a password row; 'gone' is not reinserted
    cur.executemany.assert_any_call(
        "INSERT INTO radcheck (username, attribute, op, value) "
        "VALUES (%s, 'Cleartext-Password', ':=', %s)",
        [("alice", "pw-a")],
    )
