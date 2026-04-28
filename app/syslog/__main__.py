"""Entry point for running the syslog listener as a module.

Usage: python -m app.syslog
"""

import asyncio

from app.syslog.listener import main

if __name__ == "__main__":
    asyncio.run(main())
