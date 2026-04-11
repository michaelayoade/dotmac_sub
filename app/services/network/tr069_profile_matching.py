"""Shared helpers for matching OLT TR-069 profiles to ACS servers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def normalize_acs_url(value: str | None) -> str:
    """Normalize ACS URLs for profile matching."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    if scheme and netloc:
        return f"{scheme}://{netloc}{path}"
    return raw.rstrip("/").lower()


def acs_host(value: str | None) -> str:
    """Extract the ACS host from a URL or raw host string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.hostname:
        return parsed.hostname.lower()
    return raw.split("//", 1)[-1].split(":", 1)[0].split("/", 1)[0].lower()


def match_tr069_profile(
    profiles: list[Any],
    *,
    acs_url: str,
    acs_username: str = "",
) -> Any | None:
    """Find the OLT TR-069 profile that best matches the target ACS.

    Username is treated as a strong check when both sides are present, but OLTs
    may omit it from parsed profile detail. In that case, URL match is enough.
    """
    normalized_url = normalize_acs_url(acs_url)
    target_host = acs_host(acs_url)
    normalized_username = str(acs_username or "").strip()

    for profile in profiles:
        profile_url = normalize_acs_url(getattr(profile, "acs_url", None))
        if not profile_url or profile_url != normalized_url:
            continue
        profile_username = str(getattr(profile, "acs_username", "") or "").strip()
        if not normalized_username or not profile_username:
            return profile
        if profile_username == normalized_username:
            return profile

    if target_host:
        for profile in profiles:
            profile_url = normalize_acs_url(getattr(profile, "acs_url", None))
            if profile_url and acs_host(profile_url) == target_host:
                return profile
    return None
