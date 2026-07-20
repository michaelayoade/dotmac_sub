"""Guard the retired VAS domain against accidental runtime resurrection."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REMOVED_RUNTIME_PATHS = (
    "app/models/vas.py",
    "app/schemas/vas.py",
    "app/services/vas_wallet.py",
    "app/services/vas_purchases.py",
    "app/services/vas_refunds.py",
    "app/services/vas_admin_commands.py",
    "app/services/vtpass.py",
    "app/tasks/vas.py",
    "app/web/admin/vas.py",
    "app/web/customer/wallet.py",
    "app/web/customer/bills.py",
    "mobile/lib/src/features/billing/pay_bills_screen.dart",
    "mobile/lib/src/features/billing/wallet_screen.dart",
    "mobile/lib/src/features/reseller/reseller_vas_screen.dart",
    "mobile/lib/src/models/vas.dart",
    "mobile/lib/src/models/wallet.dart",
    "mobile/lib/src/repositories/wallet_repository.dart",
)


def test_vas_runtime_paths_stay_retired() -> None:
    present = [path for path in REMOVED_RUNTIME_PATHS if (PROJECT_ROOT / path).exists()]
    assert not present, "Retired VAS runtime paths returned:\n  " + "\n  ".join(present)


def test_vas_routes_tasks_and_imports_stay_absent() -> None:
    violations: list[str] = []
    roots = ("app/api", "app/web", "app/tasks", "app/services", "mobile/lib/src")
    forbidden = (
        "app.tasks.vas",
        "app.models.vas",
        "app.schemas.vas",
        "/vas",
        "/wallet",
        "vtpass",
    )
    for root in roots:
        for path in (PROJECT_ROOT / root).rglob("*"):
            if path.suffix not in {".py", ".dart"}:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: {marker}")
    assert not violations, "Retired VAS runtime reference returned:\n  " + "\n  ".join(
        violations
    )
