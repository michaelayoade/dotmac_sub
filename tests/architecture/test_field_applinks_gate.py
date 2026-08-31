from pathlib import Path

import pytest

from scripts.check_field_applinks import Report, check_callback_access_log

CALLBACK = "/oidc/field/callback"
CONF = Path("deploy/links.dotmac.io/links.dotmac.io.conf")


def _failures(callback_directive: str) -> list[str]:
    report = Report()
    check_callback_access_log(
        report,
        CONF,
        f"""
        server {{
            access_log /var/log/nginx/links.access.log;
            location = /.well-known/assetlinks.json {{
                access_log /var/log/nginx/links.association.log;
            }}
            location = {CALLBACK} {{
                {callback_directive}
            }}
        }}
        """,
        CALLBACK,
    )
    return report.failures


def test_callback_disables_access_log_without_disabling_association_log() -> None:
    assert _failures("access_log off;") == []


@pytest.mark.parametrize(
    "directive",
    [
        "",
        "# access_log off;",
        "access_log /var/log/nginx/links.callback.log;",
        "access_log off;\naccess_log /var/log/nginx/links.callback.log;",
    ],
)
def test_callback_log_guard_is_sensitive_to_unsafe_forms(directive: str) -> None:
    failures = _failures(directive)

    assert len(failures) == 1
    assert "authorization responses carry code and state" in failures[0]
