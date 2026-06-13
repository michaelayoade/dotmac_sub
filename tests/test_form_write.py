"""The `form_write` transaction-hygiene helper (#24/#27 follow-up).

Guards a form handler's DB write so a failure rolls the session back before the
handler's `except` re-queries to re-render — preventing a poisoned-transaction
500. See app/db.py:form_write.
"""

from unittest.mock import MagicMock

import pytest

from app.db import form_write


def test_form_write_rolls_back_on_error():
    db = MagicMock()
    with pytest.raises(ValueError):
        with form_write(db):
            raise ValueError("boom")
    db.rollback.assert_called_once()


def test_form_write_reraises_original_exception():
    db = MagicMock()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with form_write(db):
            raise Boom()


def test_form_write_noop_on_success():
    db = MagicMock()
    with form_write(db):
        pass
    db.rollback.assert_not_called()
    db.commit.assert_not_called()  # the helper never commits; that's the caller's job
