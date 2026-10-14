"""Syslog listener package for event-driven ONT autofind.

Provides a UDP syslog listener that receives ONTAUTOFIND events from Huawei OLTs
and triggers autofind scans in near real-time.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.syslog.listener import SyslogListener, main
    from app.syslog.parsers import HuaweiSyslogParser, SyslogMessage

__all__ = ["SyslogListener", "HuaweiSyslogParser", "SyslogMessage", "main"]


def __getattr__(name: str) -> object:
    """Keep the health CLI independent of parser/ORM startup dependencies."""
    if name in ("SyslogListener", "main"):
        from app.syslog import listener

        return getattr(listener, name)
    if name in ("HuaweiSyslogParser", "SyslogMessage"):
        from app.syslog import parsers

        return getattr(parsers, name)
    raise AttributeError(name)
