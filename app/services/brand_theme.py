"""Generate an 11-stop colour scale from a single brand hex colour.

Pure helper used by the public ``/branding/theme.css`` route so the brand
primary colour can be themed at runtime without a CSS rebuild.
"""

from __future__ import annotations

import re

DEFAULT_HEX = "#206a07"
DEFAULT_SECONDARY_HEX = "#06b6d4"
SEMANTIC_TONES = ("positive", "info", "warning", "negative", "neutral")
DEFAULT_SEMANTIC_COLORS = {
    "positive": "#15803d",
    "info": "#1d4ed8",
    "warning": "#a16207",
    "negative": "#b91c1c",
    "neutral": "#475569",
}
COLOR_SCALE_STEPS = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)

# Tailwind palette names still appear in older templates. They are compatibility
# aliases only: the runtime brand stylesheet remaps every shade to a canonical
# identity or semantic role so those classes cannot become a second colour
# authority. New UI code should author primary/accent/semantic tokens directly.
LEGACY_TAILWIND_PALETTE_ROLES = {
    "red": "semantic-negative",
    "rose": "semantic-negative",
    "orange": "semantic-warning",
    "amber": "semantic-warning",
    "yellow": "semantic-warning",
    "lime": "semantic-positive",
    "green": "semantic-positive",
    "emerald": "semantic-positive",
    "sky": "semantic-info",
    "blue": "semantic-info",
    "cyan": "accent",
    "teal": "accent",
    "indigo": "primary",
    "violet": "primary",
    "purple": "primary",
    "fuchsia": "accent",
    "pink": "accent",
}

# Ordered, brand-derived colours for charts, maps, and other categorical data.
# The keys are emitted as --color-data-1 ... --color-data-7 by theme.css.
CATEGORICAL_COLOR_ROLES = (
    "primary",
    "accent",
    "semantic-info",
    "semantic-positive",
    "semantic-warning",
    "semantic-negative",
    "semantic-neutral",
)
MIN_SEMANTIC_TEXT_CONTRAST = 4.5
_SIX_DIGIT_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

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


def _relative_luminance(hex_color: str) -> float:
    r, g, b = _parse_hex(hex_color)

    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG relative-luminance contrast ratio for two hex colors."""
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def semantic_color_contrast_ratios(hex_color: str) -> tuple[float, float]:
    """Return light/dark text contrast produced by the semantic scale."""
    scale = generate_scale(hex_color)
    return (
        contrast_ratio(scale[700], scale[50]),
        contrast_ratio(scale[300], scale[900]),
    )


def is_accessible_semantic_color(hex_color: str) -> bool:
    """Whether a semantic seed keeps status text at WCAG AA in both themes."""
    if not _SIX_DIGIT_HEX.fullmatch(str(hex_color)):
        return False
    return all(
        ratio >= MIN_SEMANTIC_TEXT_CONTRAST
        for ratio in semantic_color_contrast_ratios(hex_color)
    )
