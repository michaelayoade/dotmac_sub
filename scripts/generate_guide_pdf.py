from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guide_pdf_common import GuideConfig, generate_guide

def main():
    generate_guide(
        GuideConfig(
            markdown_path=Path("docs/USER_GUIDE.md"),
            screenshots_dir=Path("docs/guide_screenshots"),
            output_pdf=Path("docs/DotMac_Sub_User_Guide.pdf"),
            output_html=Path("docs/DotMac_Sub_User_Guide.html"),
            html_title="DotMac Sub — User Guide",
            cover_title="User Guide",
            cover_subtitle="Task-oriented walkthrough for daily product use",
            cover_tagline="Core subscriber, billing, network, and portal workflows for operators and technical staff.",
            cover_version="Version 1.1 · March 2026",
            cover_audience="Audience: ISP administrators and technical staff",
            footer_title="DotMac Sub User Guide",
            accent_color="#2563eb",
        )
    )


if __name__ == "__main__":
    main()
