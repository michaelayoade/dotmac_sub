"""Compatibility layer for NAS services under the catalog namespace."""

import logging

from app.services.nas import *  # noqa: F401,F403

logger = logging.getLogger(__name__)
