"""Studio's exact lineage is installed while Sub's live notifier stays legacy."""

import configparser
import tomllib
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from dotmac_template_studio import __version__, migrations_dir

ROOT = Path(__file__).resolve().parents[2]


def test_template_studio_exact_pin_lock_and_lineage() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    with (ROOT / "poetry.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    assert "dotmac-template-studio==0.2.0a5" in pyproject["project"]["dependencies"]
    assert pyproject["tool"]["poetry"]["dependencies"]["dotmac-template-studio"] == {
        "version": "0.2.0a5",
        "source": "forgejo",
    }
    package = next(
        item for item in lock["package"] if item["name"] == "dotmac-template-studio"
    )
    assert package["version"] == __version__ == "0.2.0a5"
    assert {item["hash"] for item in package["files"]} == {
        "sha256:f9ec5458494375ef6fe5df83c58ab6451534d5a0e6edcd952fb878a52358ec7d",
        "sha256:b33e4a1fe7d54a9565bde72784ff1b4b53fde27380dc6edb2c648809dfb16ff5",
    }
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "alembic.ini")
    assert (
        "dotmac_template_studio.migrations:versions"
        in parser["alembic"]["version_locations"].split()
    )
    assert migrations_dir().is_dir()
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    root = script.get_revision("ts_0001_templates")
    head = script.get_revision("ts_0002_notify_identity")
    assert set(root.dependencies) == {
        "545_tenant_scope_catalog_prereq",
        "546_module_db_roles_prereq",
    }
    assert head.down_revision == root.revision


def test_expand_mounts_no_studio_routes_or_live_notification_renderer() -> None:
    main = (ROOT / "app/main.py").read_text()
    handler = (ROOT / "app/services/events/handlers/notification.py").read_text()
    assert "dotmac_template_studio.router" not in main
    assert "dotmac_template_studio.web" not in main
    assert "dotmac_template_studio" not in handler
    assert "payment_email_episodes" not in handler
