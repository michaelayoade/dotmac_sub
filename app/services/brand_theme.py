"""Generate an 11-stop colour scale from a single brand hex colour.

Pure helper used by the public ``/branding/theme.css`` route so the brand
primary colour can be themed at runtime without a CSS rebuild.
"""

from __future__ import annotations

DEFAULT_HEX = "#206a07"

# step -> (mix_ratio, mix_toward_white)
# Light steps mix the base toward white; dark steps mix toward black.
# 600 is the base colour itself.
_SCALE_RATIOS: dict[int, tuple[float, bool]] = {
    50: (0.92, True),
    100: (0.84, True),
    200: (0.68, True),
    300: (0.50, True),
    400: (0.26, True),
    500: (0.10, True),
    600: (0.00, True),
    700: (0.18, False),
    800: (0.34, False),
    900: (0.50, False),
    950: (0.62, False),
}


def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    """Parse a 3- or 6-digit hex string to an (r, g, b) tuple.

    Falls back to the default brand green on any malformed input.
    """
    try:
        raw = str(hex_color).strip().lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) != 6:
            raise ValueError("hex must be 3 or 6 digits")
        r = int(raw[0:2], 16)
        g = int(raw[2:4], 16)
        b = int(raw[4:6], 16)
        return r, g, b
    except (ValueError, TypeError, AttributeError):
        clean = DEFAULT_HEX.lstrip("#")
        return (
            int(clean[0:2], 16),
            int(clean[2:4], 16),
            int(clean[4:6], 16),
        )


def _clamp(value: float) -> int:
    return max(0, min(255, round(value)))


def _mix(channel: int, ratio: float, toward_white: bool) -> int:
    target = 255 if toward_white else 0
    return _clamp(channel + (target - channel) * ratio)


def generate_scale(hex_color: str) -> dict[int, str]:
    """Return {50:'#..',...,950:'#..'} from a base hex.

    Falls back to ``#206a07`` on bad input.
    """
    r, g, b = _parse_hex(hex_color)
    scale: dict[int, str] = {}
    for step, (ratio, toward_white) in _SCALE_RATIOS.items():
        nr = _mix(r, ratio, toward_white)
        ng = _mix(g, ratio, toward_white)
        nb = _mix(b, ratio, toward_white)
        scale[step] = f"#{nr:02x}{ng:02x}{nb:02x}"
    return scale
