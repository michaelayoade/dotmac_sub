import json
import logging
import sys

from app.logging import (
    JsonLogFormatter,
    SensitiveQueryFilter,
    install_log_redaction,
    redact_routeros_credentials,
    sanitize_exception,
)


def test_json_log_formatter_includes_dynamic_extra_fields():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="critical_path_event",
        args=(),
        exc_info=None,
    )
    record.operation_id = "op-123"
    record.operation_type = "ont_authorize"
    record.details = {"fsp": "0/1/3", "serial": "UBNT-F9AA7344"}

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "critical_path_event"
    assert payload["operation_id"] == "op-123"
    assert payload["operation_type"] == "ont_authorize"
    assert payload["details"] == {"fsp": "0/1/3", "serial": "UBNT-F9AA7344"}


def test_json_log_formatter_keeps_safe_classifier_evidence():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.services.ai_intake",
        level=logging.WARNING,
        pathname=__file__,
        lineno=30,
        msg="ai intake classifier unavailable",
        args=(),
        exc_info=None,
    )
    record.event = "ai_intake_classifier_unavailable"
    record.classifier_attempt_status = "invalid_output"
    record.classifier_failure_reason = "classifier_invalid_output"
    record.classifier_failure_kind = "schema_validation_failure"
    record.classifier_retry_count = 1
    record.classifier_retry_limit = 2
    record.provider = "configured-provider"
    record.model = "configured-model"

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "ai_intake_classifier_unavailable"
    assert payload["classifier_attempt_status"] == "invalid_output"
    assert payload["classifier_failure_reason"] == "classifier_invalid_output"
    assert payload["classifier_failure_kind"] == "schema_validation_failure"
    assert payload["classifier_retry_count"] == 1
    assert payload["classifier_retry_limit"] == 2
    assert payload["provider"] == "configured-provider"
    assert payload["model"] == "configured-model"


def test_sensitive_query_filter_redacts_formatted_uvicorn_request_line():
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=30,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1", "/ws/inbox?token=header.payload.signature&keep=1"),
        exc_info=None,
    )

    assert SensitiveQueryFilter().filter(record) is True
    rendered = record.getMessage()
    assert "header.payload.signature" not in rendered
    assert "token=<redacted>" in rendered
    assert "keep=1" in rendered


def test_redact_routeros_credentials_handles_the_bytes_repr_form():
    # Shape observed in production: routeros_api embeds the raw /login word,
    # including the cleartext password, in the exception text it raises, and
    # that word is itself inside a Python bytes-repr string.
    secret = "SuperSecret123"
    raw = (
        'Error "invalid user name or password ..." executing command '
        f"b'/login =name=Eagle_API =password={secret} .tag=1'"
    )
    assert secret in raw  # sensitivity proof: the fixture really carries it

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert "=password=<redacted>" in redacted
    # Unrelated command fields survive untouched.
    assert "=name=Eagle_API" in redacted
    assert ".tag=1" in redacted


def test_redact_routeros_credentials_stops_at_a_trailing_quote_not_just_space():
    # A password that is the last word before the closing repr quote (no
    # trailing space) must not have the quote consumed into the match.
    secret = "abc123"
    raw = f"b'/login =name=x =password={secret}'"

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert redacted == "b'/login =name=x =password=<redacted>'"


def test_redact_routeros_credentials_also_covers_the_secret_field():
    secret = "TopSecretValue"
    raw = f"executing b'/login =name=x =secret={secret} .tag=2'"

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert "=secret=<redacted>" in redacted


def test_redact_routeros_credentials_does_not_touch_unrelated_password_text():
    # Near-miss: the word "password" appears in ordinary prose, not as
    # RouterOS API word syntax (`=password=...`). It must be left alone.
    text = 'Error "invalid user name or password" for user bob'

    assert redact_routeros_credentials(text) == text


def test_redact_routeros_credentials_password_containing_an_embedded_double_quote():
    # A password containing `"` must not let the match stop early at that
    # character (the old regex's bug) — verified against a REAL bytes repr,
    # since that is the actual shape routeros_api produces.
    secret = 'my"pass'
    command = f"/login =name=x =password={secret} .tag=1".encode()
    raw = repr(command)
    assert secret in raw  # sensitivity proof

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert "=password=<redacted>" in redacted
    assert ".tag=1" in redacted


def test_redact_routeros_credentials_password_containing_an_embedded_single_quote():
    # A password containing `'` makes Python's bytes repr switch to a `"`
    # delimiter — a different real shape than the double-quote case above.
    secret = "my'pass"
    command = f"/login =name=x =password={secret} .tag=1".encode()
    raw = repr(command)
    assert raw.startswith('b"')  # confirms the "-delimited repr shape
    assert secret in raw  # sensitivity proof

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert "=password=<redacted>" in redacted
    assert ".tag=1" in redacted


def test_redact_routeros_credentials_password_containing_a_space():
    # A password containing a literal space must not be truncated at that
    # space (the old regex's other bug) when nothing but the closing repr
    # quote follows it.
    secret = "my pass"
    command = f"/login =name=x =password={secret}".encode()
    raw = repr(command)
    assert secret in raw  # sensitivity proof

    redacted = redact_routeros_credentials(raw)

    assert secret not in redacted
    assert "=password=<redacted>" in redacted


def test_sanitize_exception_redacts_and_falls_back_to_type_name():
    assert sanitize_exception(TimeoutError()) == "TimeoutError"

    exc = RuntimeError(
        "failure executing command b'/login =name=x =password=hunter2 .tag=1'"
    )
    assert "hunter2" in str(exc)  # sensitivity proof

    assert sanitize_exception(exc) == (
        "failure executing command b'/login =name=x =password=<redacted> .tag=1'"
    )


def test_sensitive_query_filter_redacts_an_exception_passed_as_a_log_argument():
    # A common call shape across the RouterOS call sites:
    # logger.warning("... %s", name, exc)  — the exception is a %s arg, not
    # part of record.msg, so it bypasses plain string redaction unless the
    # filter special-cases exception-typed args.
    secret = "hunter2"
    exc = RuntimeError(f"executing command b'/login =name=x =password={secret} .tag=1'")
    record = logging.LogRecord(
        name="app.services.enforcement",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Address-list %s: API fallback failed for %s: %s",
        args=("remove", "BNG-1", exc),
        exc_info=None,
    )

    assert SensitiveQueryFilter().filter(record) is True
    rendered = record.getMessage()

    assert secret not in rendered
    assert "=password=<redacted>" in rendered
    assert ".tag=1" in rendered


def test_sensitive_query_filter_redacts_rendered_exc_info_text():
    secret = "hunter2"
    try:
        raise RuntimeError(f"boom =password={secret} .tag=1")
    except RuntimeError:
        record = logging.LogRecord(
            name="app.services.enforcement",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    assert SensitiveQueryFilter().filter(record) is True
    payload = json.loads(JsonLogFormatter().format(record))

    assert secret not in payload["exception"]
    assert "=password=<redacted>" in payload["exception"]


def test_json_log_formatter_recomputing_exc_info_without_the_filter_would_leak():
    # Sensitivity proof for the previous test: JsonLogFormatter recomputing
    # the traceback straight from record.exc_info (skipping the filter's
    # exc_text cache) reproduces the raw leak.
    secret = "hunter2"
    try:
        raise RuntimeError(f"boom =password={secret} .tag=1")
    except RuntimeError:
        record = logging.LogRecord(
            name="app.services.enforcement",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    # No filter applied — this is the pre-fix / near-miss behaviour.
    payload = json.loads(JsonLogFormatter().format(record))

    assert secret in payload["exception"]


def test_json_log_formatter_redacts_credentials_in_extra_fields():
    # An `extra={...}` value can itself be an exception object (e.g. a
    # captured cause) or a plain string containing RouterOS API syntax; both
    # go through `_json_safe`, which must redact them the same as msg/args.
    secret = "hunter2"
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="fallback exhausted",
        args=(),
        exc_info=None,
    )
    record.cause = RuntimeError(f"failure =password={secret} .tag=1")
    record.raw_command = f"b'/login =password={secret} .tag=1'"
    assert secret in str(record.cause)  # sensitivity proof

    payload = json.loads(formatter.format(record))

    assert secret not in payload["cause"]
    assert "=password=<redacted>" in payload["cause"]
    assert secret not in payload["raw_command"]
    assert "=password=<redacted>" in payload["raw_command"]


def test_install_log_redaction_is_idempotent_and_covers_plain_handlers():
    secret = "hunter2"
    target = logging.Logger("test.install_log_redaction")
    handler = logging.StreamHandler()
    target.addHandler(handler)

    install_log_redaction(target)
    install_log_redaction(target)  # second call must not add a second filter

    assert sum(isinstance(f, SensitiveQueryFilter) for f in handler.filters) == 1

    record = logging.LogRecord(
        name=target.name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=f"failure =password={secret} .tag=1",
        args=(),
        exc_info=None,
    )
    for f in handler.filters:
        assert f.filter(record) is True
    assert secret not in record.getMessage()
