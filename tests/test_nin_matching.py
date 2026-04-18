from datetime import date
from types import SimpleNamespace

from app.services.nin_matching import match_subscriber_nin_response


def _subscriber(**overrides):
    values = {
        "first_name": "Aisha",
        "last_name": "Okafor",
        "date_of_birth": date(1990, 5, 14),
        "phone": "08031234567",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_nin_match_accepts_surname_first_with_middle_name():
    result = match_subscriber_nin_response(
        _subscriber(),
        {
            "full_name": "OKAFOR AISHA CHINENYE",
            "date_of_birth": "1990-05-14",
            "phone_number": "2348031234567",
        },
    )

    assert result["is_match"] is True
    assert result["match_score"] == 90


def test_nin_match_accepts_common_dob_formats():
    result = match_subscriber_nin_response(
        _subscriber(),
        {
            "full_name": "Aisha Okafor",
            "date_of_birth": "14/05/1990",
            "phone_number": "08031234567",
        },
    )

    assert result["is_match"] is True
    assert result["match_score"] == 100


def test_nin_match_still_fails_when_dob_differs():
    result = match_subscriber_nin_response(
        _subscriber(),
        {
            "full_name": "OKAFOR AISHA CHINENYE",
            "date_of_birth": "1991-05-14",
            "phone_number": "08031234567",
        },
    )

    assert result["is_match"] is False
    assert result["match_score"] == 60
