"""app/logging.py is the ONLY place allowed to contain the raw RouterOS API
credential marker ``=password=``. Every other module must redact through the
shared ``sanitize_exception``/``redact_routeros_credentials`` helpers instead
of growing its own copy of the pattern (the defect this whole change fixes:
per-module duplicate/weaker regexes that leaked a cleartext password to Loki).

Scope: this guards against a competing or weaker redaction pattern being
reintroduced. It cannot see a NEW call site that simply forgets to call
``sanitize_exception`` — the secret exists only in runtime exception text,
never in source. That case is covered by the logging-layer filter
(``SensitiveQueryFilter``) and the Sentry ``before_send`` scrubber, not here.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"
OWNER = APP_ROOT / "logging.py"
MARKER = "=password="


def _offenders(app_root: Path, owner: Path) -> dict[str, list[int]]:
    """Map each non-owner ``*.py`` file under ``app_root`` to its marker lines."""
    found: dict[str, list[int]] = {}
    for path in sorted(app_root.rglob("*.py")):
        if path == owner:
            continue
        lines = [
            lineno
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            )
            if MARKER in line
        ]
        if lines:
            found[str(path.relative_to(app_root.parent))] = lines
    return found


def test_only_app_logging_contains_the_raw_routeros_password_marker():
    offenders = _offenders(APP_ROOT, OWNER)

    assert not offenders, (
        "Only app/logging.py may contain the literal RouterOS API credential "
        f"marker '{MARKER}'; found it in: {offenders}. Redact via "
        "app.logging.sanitize_exception/redact_routeros_credentials instead "
        "of matching or reproducing the pattern locally."
    )


def test_the_scanner_catches_a_planted_marker_and_exempts_only_the_owner(tmp_path):
    # Sensitivity proof through the SAME discovery/exclusion path the real
    # test uses: a planted offender under a nested package is flagged, while
    # an owner file carrying the marker is not.
    app_root = tmp_path / "app"
    (app_root / "services").mkdir(parents=True)
    owner = app_root / "logging.py"
    owner.write_text('PATTERN = "=password="\n', encoding="utf-8")
    planted = app_root / "services" / "planted_offender.py"
    planted.write_text(
        "import re\n_PASSWORD_RE = re.compile(r'=password=[^ ]*')\n",
        encoding="utf-8",
    )

    assert _offenders(app_root, owner) == {"app/services/planted_offender.py": [2]}


def test_the_real_owner_module_does_carry_the_marker():
    # Near-miss: the exemption is load-bearing only because the owner really
    # contains the marker; if that ever stops being true the exclusion is dead.
    assert MARKER in OWNER.read_text(encoding="utf-8")
