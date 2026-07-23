"""Pure transforms for the one-time Splynx -> Sub geospatial backfill.

Splynx (the legacy ISP system, being retired) is the historical source of the
coordinates Sub's own tables never captured: POP/BTS site coordinates
(``network_sites.gps``) and per-subscriber install coordinates
(``customers.gps``). Sub owns those facts going forward; this backfill simply
lands the history in the right place.

This module holds only the *pure*, deterministic transforms — GPS string
parsing (with range validation and axis-swap repair) and site-name
normalisation for matching. It performs no I/O.

Ownership split:
* The spatial writes into ``pop_sites`` / ``addresses`` (and the projection into
  ``geo_locations``) are owned by ``gis.spatial_sync`` (``app.services.gis_sync``).
* The connection to the Splynx restore and the Splynx->Sub entity matching live
  in the one-off runner ``scripts/migration/import_splynx_geo.py``.

Splynx GPS strings are dirty in three known ways, all handled here:
  1. an optional trailing altitude component (``"9.05,7.48,0"``);
  2. axis-swapped pairs stored as ``"lng,lat"`` instead of ``"lat,lng"``;
  3. free whitespace / stray separators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Broad plausibility envelope for *either* axis anywhere in Nigeria. Used only
# to reject non-Nigeria garbage before orientation.
NG_MIN = 2.5
NG_MAX = 14.5

# Dotmac's served footprint is Abuja + Lagos (+ nearby SW Nigeria). Across all
# of it latitude strictly exceeds longitude (Abuja ~9.05 lat / ~7.48 lng; Lagos
# ~6.5 lat / ~3.3 lng). Because the per-axis envelopes overlap, this
# lat > lng invariant — not the envelope — is what lets us deterministically
# repair axis-swapped rows. Points landing outside the tighter box below are
# still accepted but flagged for human review.
TIGHT_LAT_MIN, TIGHT_LAT_MAX = 5.8, 9.8
TIGHT_LNG_MIN, TIGHT_LNG_MAX = 2.7, 8.2


@dataclass(frozen=True)
class ParsedPoint:
    """A validated coordinate oriented to (latitude, longitude)."""

    latitude: float
    longitude: float
    swapped: bool  # source pair was (lng, lat); we corrected it
    needs_review: bool  # oriented point falls outside the tight Dotmac box


def parse_gps(raw: str | None) -> ParsedPoint | None:
    """Parse a Splynx GPS string to a validated ``ParsedPoint`` or ``None``.

    Returns ``None`` for empty, non-numeric, or out-of-envelope values. Accepts
    an optional trailing altitude token. Orients the pair so ``latitude >=
    longitude`` (the Dotmac-footprint invariant), which repairs the common
    ``"lng,lat"`` rows even though the per-axis envelopes overlap.
    """
    if not raw:
        return None
    tokens = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    nums: list[float] = []
    for token in tokens:
        try:
            nums.append(float(token))
        except ValueError:
            return None
        if len(nums) == 2:
            break
    if len(nums) < 2:
        return None
    a, b = nums[0], nums[1]
    if not (NG_MIN <= a <= NG_MAX and NG_MIN <= b <= NG_MAX):
        return None
    if a >= b:
        latitude, longitude, swapped = a, b, False
    else:
        latitude, longitude, swapped = b, a, True
    needs_review = not (
        TIGHT_LAT_MIN <= latitude <= TIGHT_LAT_MAX
        and TIGHT_LNG_MIN <= longitude <= TIGHT_LNG_MAX
    )
    return ParsedPoint(
        latitude=latitude,
        longitude=longitude,
        swapped=swapped,
        needs_review=needs_review,
    )


# Tokens that appear in Splynx BTS/site titles but not in Sub POP names (or vice
# versa), stripped before matching. "Garki-Abj-Bts" and Sub "Garki" both reduce
# to "garki".
_SITE_STOPWORDS = frozenset(
    {"bts", "abj", "abuja", "lag", "lagos", "site", "pop", "base", "station"}
)


def normalize_site_name(name: str | None) -> str:
    """Reduce a POP/BTS site name to a comparison key.

    Lowercases, drops punctuation, and removes region/BTS stopwords so a Splynx
    ``network_sites.title`` can be matched to a Sub ``pop_sites.name``. Returns
    an empty string when nothing distinctive remains.
    """
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    parts = [p for p in cleaned.split() if p and p not in _SITE_STOPWORDS]
    return " ".join(parts)


def keys_match(site_key: str, pop_key: str) -> bool:
    """True when two normalized site keys refer to the same place.

    Exact equality, or one key's tokens are a subset of the other's (so Splynx
    "boi asokoro" matches Sub "asokoro"). Empty keys never match.
    """
    if not site_key or not pop_key:
        return False
    site_tokens = set(site_key.split())
    pop_tokens = set(pop_key.split())
    return site_tokens <= pop_tokens or pop_tokens <= site_tokens


def detect_region(title: str | None) -> str | None:
    """Map a Splynx BTS title's locality token to a Sub region."""
    lowered = (title or "").lower()
    if "lag" in lowered or "lagos" in lowered:
        return "Lagos"
    if "abj" in lowered or "abuja" in lowered:
        return "Abuja"
    return None


def clean_pop_name(title: str | None) -> str:
    """Human POP name for a created site: normalized key, title-cased."""
    return normalize_site_name(title).title() or (title or "").strip()
