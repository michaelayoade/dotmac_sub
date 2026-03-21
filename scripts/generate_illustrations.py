#!/usr/bin/env python3
"""Generate ISP-themed illustrations using Gemini image generation.

Creates contextual illustrations for empty states, login pages,
and onboarding banners across the DotMac Sub admin and customer portals.

Usage:
    python scripts/generate_illustrations.py --api-key YOUR_GEMINI_KEY
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("static/illustrations")

# Design system colors for prompt context
STYLE_PREFIX = (
    "Minimal flat vector illustration, clean line art style, "
    "soft blue and indigo color palette (#3b82f6, #6366f1) with white background, "
    "simple geometric shapes, no text, no people's faces, "
    "modern tech/ISP aesthetic, suitable for light and dark UI themes, "
    "512x512 pixels, centered composition with generous whitespace. "
)

# Illustrations to generate
ILLUSTRATIONS = [
    # Tier 1: Customer Portal
    {
        "name": "customer-login",
        "prompt": STYLE_PREFIX + "A fiber optic cable connecting to a glowing router device with signal waves radiating outward, representing internet connectivity and customer access. Subtle gradient from blue to indigo.",
        "size": "512x512",
    },
    {
        "name": "no-invoices",
        "prompt": STYLE_PREFIX + "A receipt or invoice document with a checkmark, floating gently, with a few small coins nearby. Represents billing completed or no pending invoices. Calm, reassuring tone.",
        "size": "512x512",
    },
    {
        "name": "no-tickets",
        "prompt": STYLE_PREFIX + "A headset with a speech bubble containing a checkmark, representing resolved support or no open tickets. Clean, minimal, professional ISP support aesthetic.",
        "size": "512x512",
    },
    {
        "name": "no-notifications",
        "prompt": STYLE_PREFIX + "A bell icon in a serene resting state, with small ZZZ marks indicating quiet/peace. No pending notifications. Calm and minimal.",
        "size": "512x512",
    },
    {
        "name": "no-speedtests",
        "prompt": STYLE_PREFIX + "A speedometer gauge at rest position with a small rocket icon nearby, ready to launch. Represents speed testing that hasn't been run yet. Dynamic but calm.",
        "size": "512x512",
    },
    {
        "name": "no-work-orders",
        "prompt": STYLE_PREFIX + "A clipboard with a blank checklist and a small wrench tool beside it. Represents no pending work orders or field service tasks. Clean, organized feel.",
        "size": "512x512",
    },
    # Tier 2: Admin Dashboard & Monitoring
    {
        "name": "network-monitoring",
        "prompt": STYLE_PREFIX + "A network topology with interconnected nodes (router, switch, access point, ONT) forming a mesh. Signal strength bars and a small heartbeat pulse line. ISP network monitoring aesthetic.",
        "size": "512x512",
    },
    {
        "name": "welcome-dashboard",
        "prompt": STYLE_PREFIX + "A dashboard screen showing graphs, KPI cards, and a rising trend arrow. Represents a management dashboard coming to life. Professional, data-driven aesthetic.",
        "size": "512x512",
    },
    {
        "name": "device-sync",
        "prompt": STYLE_PREFIX + "Two devices (a server rack and a router) with circular arrows between them representing synchronization. Cloud element above. Clean tech sync illustration.",
        "size": "512x512",
    },
    # Tier 3: Admin Empty States (generic categories)
    {
        "name": "no-devices",
        "prompt": STYLE_PREFIX + "A fiber optic ONT device outline with a dotted border and a plus icon, representing an empty device list waiting for first device to be added. Blueprint/schematic style.",
        "size": "512x512",
    },
    {
        "name": "no-subscribers",
        "prompt": STYLE_PREFIX + "A person silhouette with a WiFi signal above their head and a plus icon. Represents an empty subscriber list. Friendly, inviting.",
        "size": "512x512",
    },
    {
        "name": "no-payments",
        "prompt": STYLE_PREFIX + "A wallet or payment card with a small clock icon, representing pending or no payment history. Clean financial aesthetic.",
        "size": "512x512",
    },
    {
        "name": "no-data",
        "prompt": STYLE_PREFIX + "An empty chart with a gentle upward arrow outline, ready for data. Represents analytics or reports with no data yet. Clean, minimal.",
        "size": "512x512",
    },
    {
        "name": "search-empty",
        "prompt": STYLE_PREFIX + "A magnifying glass over an empty document with a subtle question mark. Represents no search results found. Clean and minimal.",
        "size": "512x512",
    },
    {
        "name": "all-clear",
        "prompt": STYLE_PREFIX + "A shield with a checkmark inside, radiating gentle glow. Represents all clear / no alarms / healthy status. Reassuring, professional.",
        "size": "512x512",
    },
]


def generate_with_gemini(prompt: str, api_key: str, model: str = "gemini-2.0-flash-preview-image-generation") -> bytes | None:
    """Generate an image using Gemini API."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )

        candidates = response.candidates or []
        if not candidates:
            logger.warning("No candidates in Gemini response for prompt: %s", prompt[:60])
            return None

        content = candidates[0].content
        parts = content.parts if content is not None and content.parts is not None else []
        for part in parts:
            if part.inline_data is not None:
                return part.inline_data.data

        logger.warning("No image in response for prompt: %s", prompt[:60])
        return None
    except Exception as exc:
        logger.error("Gemini API error: %s", exc)
        return None


def optimize_image(data: bytes, output_path: Path, target_size: int = 512) -> None:
    """Resize and optimize a PNG image."""
    import io

    from PIL import Image

    img: Image.Image = Image.open(io.BytesIO(data))

    # Resize to target
    if img.width != target_size or img.height != target_size:
        img = img.resize((target_size, target_size), Image.Resampling.LANCZOS)

    # Convert to RGBA for transparency support
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Save as optimized PNG
    img.save(output_path, "PNG", optimize=True)

    # Also save WebP for modern browsers
    webp_path = output_path.with_suffix(".webp")
    img.save(webp_path, "WEBP", quality=85, method=6)

    png_size = output_path.stat().st_size / 1024
    webp_size = webp_path.stat().st_size / 1024
    logger.info("  Saved: %s (PNG: %.0fKB, WebP: %.0fKB)", output_path.name, png_size, webp_size)


def main():
    parser = argparse.ArgumentParser(description="Generate ISP illustrations")
    parser.add_argument("--api-key", required=True, help="Gemini API key")
    parser.add_argument("--model", default="gemini-2.0-flash-preview-image-generation", help="Gemini model")
    parser.add_argument("--only", help="Generate only this illustration name")
    parser.add_argument("--skip-existing", action="store_true", help="Skip if file exists")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    illustrations = ILLUSTRATIONS
    if args.only:
        illustrations = [i for i in ILLUSTRATIONS if i["name"] == args.only]
        if not illustrations:
            logger.error("Unknown illustration: %s", args.only)
            sys.exit(1)

    logger.info("Generating %d illustrations...", len(illustrations))

    for illust in illustrations:
        name = illust["name"]
        output_path = OUTPUT_DIR / f"{name}.png"

        if args.skip_existing and output_path.exists():
            logger.info("  Skipping %s (exists)", name)
            continue

        logger.info("  Generating: %s", name)
        data = generate_with_gemini(illust["prompt"], args.api_key, args.model)

        if data:
            optimize_image(data, output_path)
        else:
            logger.warning("  Failed to generate: %s", name)

    logger.info("Done! Illustrations saved to %s/", OUTPUT_DIR)


if __name__ == "__main__":
    main()
