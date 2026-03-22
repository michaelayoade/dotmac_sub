from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guide_pdf_common import GuideConfig, generate_guide

def main():
    generate_guide(
        GuideConfig(
            markdown_path=Path("docs/ADMIN_OPERATIONS_GUIDE.md"),
            screenshots_dir=Path("docs/guide_screenshots"),
            output_pdf=Path("docs/DotMac_Sub_Admin_Operations_Guide.pdf"),
            output_html=Path("docs/DotMac_Sub_Admin_Operations_Guide.html"),
            html_title="DotMac Sub — Administrator Operations Guide",
            cover_title="Administrator Operations Guide",
            cover_subtitle="Launch, run, and safeguard live ISP operations",
            cover_tagline="Configuration guidance, operational cadence, and change management for billing, provisioning, network, and support teams.",
            cover_version="Version 1.1 · March 2026",
            cover_audience="Audience: operations staff and system administrators",
            footer_title="DotMac Sub Admin Operations Guide",
            accent_color="#0f766e",
        )
    )


if __name__ == "__main__":
    main()
