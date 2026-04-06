import json
import logging

from app.logging import JsonLogFormatter


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
