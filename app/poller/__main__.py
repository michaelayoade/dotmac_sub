"""
Entry point for running the bandwidth poller as a module.

Usage: python -m app.poller
"""
import asyncio
from app.poller.mikrotik_poller import main

if __name__ == "__main__":
    asyncio.run(main())
