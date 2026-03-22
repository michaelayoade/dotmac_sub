"""Shared helpers for building guide HTML/PDF outputs."""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import markdown  # type: ignore[import-untyped]
from PIL import Image
from playwright.sync_api import sync_playwright

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


@dataclass(frozen=True)
class GuideConfig:
    markdown_path: Path
    screenshots_dir: Path
    output_pdf: Path
    output_html: Path
    html_title: str
    cover_title: str
    cover_subtitle: str
    cover_tagline: str
    cover_version: str
    cover_audience: str
    footer_title: str
    accent_color: str = "#0f766e"
    max_image_width: int = 1500
    image_quality: int = 72


def _resolve_image_path(src: str, screenshots_dir: Path) -> Path | None:
    for base in (screenshots_dir.parent, screenshots_dir, Path("docs")):
        candidate = base / src
        if candidate.exists():
            return candidate
    return None


def _image_to_data_uri(path: Path, max_width: int, quality: int) -> str:
    with Image.open(path) as opened_img:
        img: PILImage = opened_img
        img.load()
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize(
                (max_width, max(1, int(img.height * ratio))),
                Image.Resampling.LANCZOS,
            )

        buffer = io.BytesIO()
        has_alpha = "A" in img.getbands()
        if has_alpha:
            img.save(buffer, format="PNG", optimize=True, compress_level=9)
            mime = "image/png"
        else:
            img = img.convert("RGB")
            img.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            mime = "image/jpeg"

    data = base64.b64encode(buffer.getvalue()).decode()
    return f"data:{mime};base64,{data}"


def embed_images(html: str, screenshots_dir: Path, max_width: int, quality: int) -> str:
    """Replace local screenshot paths with optimized data URIs."""

    def replace_img(match: re.Match[str]) -> str:
        src = match.group(1)
        image_path = _resolve_image_path(src, screenshots_dir)
        if image_path is None:
            return match.group(0)
        return f'src="{_image_to_data_uri(image_path, max_width=max_width, quality=quality)}"'

    return re.sub(r'src="([^"]+\.(?:png|jpg|jpeg|webp))"', replace_img, html, flags=re.IGNORECASE)


def _build_html(config: GuideConfig, md_html: str) -> str:
    accent = config.accent_color
    accent_soft = "#ecfeff" if accent == "#0f766e" else "#eff6ff"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{config.html_title}</title>
<style>
    @page {{
        size: A4;
        margin: 18mm 14mm 20mm;
    }}
    :root {{
        --ink: #172033;
        --muted: #52607a;
        --line: #d9e2ec;
        --soft: #f8fafc;
        --accent: {accent};
        --accent-soft: {accent_soft};
    }}
    * {{ box-sizing: border-box; }}
    body {{
        font-family: "Plus Jakarta Sans", "Segoe UI", sans-serif;
        font-size: 10.5pt;
        line-height: 1.62;
        color: var(--ink);
        margin: 0;
    }}
    .cover {{
        min-height: 240mm;
        padding: 22mm 18mm;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        background:
            radial-gradient(circle at top left, rgba(255,255,255,0.92), rgba(255,255,255,0.98)),
            linear-gradient(140deg, var(--accent-soft), #ffffff 48%, #f1f5f9);
        border: 1px solid var(--line);
        border-radius: 18px;
        page-break-after: always;
    }}
    .eyebrow {{
        color: var(--accent);
        font-size: 10pt;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.14em;
    }}
    .cover h1 {{
        margin: 12mm 0 0;
        font-size: 30pt;
        line-height: 1.05;
        color: #0f172a;
    }}
    .cover .subtitle {{
        font-size: 18pt;
        color: #1e293b;
        font-weight: 600;
        margin-top: 10px;
    }}
    .cover .tagline {{
        font-size: 12.5pt;
        color: var(--muted);
        max-width: 28rem;
        margin-top: 18px;
    }}
    .cover .meta {{
        display: flex;
        gap: 16px;
        flex-wrap: wrap;
        font-size: 10pt;
        color: var(--muted);
        margin-top: 28px;
    }}
    .cover .meta span {{
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 6px 12px;
        background: rgba(255,255,255,0.78);
    }}
    h1 {{
        font-size: 24pt;
        line-height: 1.15;
        color: #0f172a;
        margin: 0 0 12px;
        padding-bottom: 8px;
        border-bottom: 3px solid var(--accent);
        page-break-before: always;
    }}
    h1:first-of-type {{ page-break-before: avoid; }}
    h2 {{
        font-size: 16pt;
        color: #12233d;
        margin: 28px 0 10px;
        padding-bottom: 6px;
        border-bottom: 1px solid var(--line);
    }}
    h3 {{
        font-size: 12.5pt;
        color: #243b53;
        margin: 18px 0 8px;
    }}
    h4 {{
        font-size: 11pt;
        color: #334e68;
        margin: 14px 0 6px;
    }}
    p, li {{ orphans: 3; widows: 3; }}
    p {{ margin: 10px 0; }}
    ul, ol {{
        margin: 10px 0 14px 20px;
        padding: 0;
    }}
    li {{ margin: 5px 0; }}
    strong {{ color: #0f172a; }}
    hr {{
        border: none;
        border-top: 2px solid var(--line);
        margin: 26px 0;
    }}
    img {{
        display: block;
        max-width: 100%;
        height: auto;
        margin: 14px 0 18px;
        border: 1px solid var(--line);
        border-radius: 10px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.07);
        page-break-inside: avoid;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 14px 0 18px;
        font-size: 9.4pt;
        page-break-inside: avoid;
    }}
    th, td {{
        border: 1px solid var(--line);
        padding: 8px 10px;
        text-align: left;
        vertical-align: top;
    }}
    th {{
        background: #eef2f7;
        color: #102a43;
        font-weight: 700;
    }}
    tr:nth-child(even) td {{
        background: #fbfcfe;
    }}
    code {{
        font-family: "SF Mono", "Fira Code", monospace;
        background: #eef2f7;
        border-radius: 4px;
        padding: 1px 5px;
        font-size: 9.2pt;
    }}
    pre {{
        background: #0f172a;
        color: #e2e8f0;
        padding: 14px 16px;
        border-radius: 10px;
        overflow: hidden;
        white-space: pre-wrap;
        font-size: 8.8pt;
        line-height: 1.5;
        page-break-inside: avoid;
    }}
    pre code {{
        background: transparent;
        color: inherit;
        padding: 0;
    }}
    blockquote {{
        margin: 14px 0 18px;
        padding: 12px 14px;
        border-left: 4px solid var(--accent);
        background: var(--accent-soft);
        color: #1f2937;
        border-radius: 0 10px 10px 0;
    }}
    a {{
        color: var(--accent);
        text-decoration: none;
    }}
    .doc {{
        counter-reset: section;
    }}
</style>
</head>
<body>
<section class="cover">
    <div>
        <div class="eyebrow">DotMac Sub</div>
        <h1>{config.cover_title}</h1>
        <div class="subtitle">{config.cover_subtitle}</div>
        <div class="tagline">{config.cover_tagline}</div>
    </div>
    <div class="meta">
        <span>{config.cover_version}</span>
        <span>{config.cover_audience}</span>
    </div>
</section>
<main class="doc">
{md_html}
</main>
</body>
</html>"""


def generate_guide(config: GuideConfig) -> None:
    md_text = config.markdown_path.read_text()
    md_html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )
    md_html = embed_images(
        md_html,
        screenshots_dir=config.screenshots_dir,
        max_width=config.max_image_width,
        quality=config.image_quality,
    )

    html = _build_html(config, md_html)
    config.output_html.write_text(html)
    print(f"HTML written: {config.output_html} ({len(html)} bytes)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--headless=new"])
        page = browser.new_page()
        page.goto(f"file://{config.output_html.resolve()}")
        page.wait_for_timeout(1500)
        page.pdf(
            path=str(config.output_pdf),
            format="A4",
            margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=(
                '<div style="text-align:center;width:100%;font-size:8px;color:#7b8794;">'
                f"{config.footer_title} · Page "
                '<span class="pageNumber"></span> of <span class="totalPages"></span>'
                "</div>"
            ),
        )
        browser.close()

    size_mb = config.output_pdf.stat().st_size / (1024 * 1024)
    print(f"PDF written: {config.output_pdf} ({size_mb:.1f} MB)")
