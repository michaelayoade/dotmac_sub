from app.services.settings_secret_cleanup import SecretCleanupResult
from scripts.one_off import migrate_secret_settings_to_openbao as command


class _SessionContext:
    session = object()

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_command_is_dry_run_by_default(monkeypatch, capsys):
    observed = {}

    def migrate(db, *, dry_run, domain, key):
        observed.update(db=db, dry_run=dry_run, domain=domain, key=key)
        return SecretCleanupResult(
            migrated=1,
            skipped=0,
            errors=[],
            migrated_keys=["auth.jwt_secret"],
            skipped_keys=[],
        )

    monkeypatch.setattr(command, "SessionLocal", _SessionContext)
    monkeypatch.setattr(command, "migrate_plaintext_secret_settings", migrate)

    assert command.main(["--domain", "auth", "--key", "jwt_secret"]) == 0
    assert observed == {
        "db": _SessionContext.session,
        "dry_run": True,
        "domain": "auth",
        "key": "jwt_secret",
    }
    output = capsys.readouterr().out
    assert "DRY-RUN: migrated=1" in output
    assert "would migrate: auth.jwt_secret" in output
    assert "rerun with --apply" in output


def test_apply_returns_failure_when_migration_reports_errors(monkeypatch, capsys):
    def migrate(db, *, dry_run, domain, key):
        assert db is _SessionContext.session
        assert dry_run is False
        assert domain is None
        assert key is None
        return SecretCleanupResult(
            migrated=0,
            skipped=1,
            errors=["auth.jwt_secret: failed to write OpenBao secret"],
            migrated_keys=[],
            skipped_keys=["auth.jwt_secret"],
        )

    monkeypatch.setattr(command, "SessionLocal", _SessionContext)
    monkeypatch.setattr(command, "migrate_plaintext_secret_settings", migrate)

    assert command.main(["--apply"]) == 1
    output = capsys.readouterr().out
    assert "APPLIED: migrated=0 skipped=1 errors=1" in output
    assert "skipped: auth.jwt_secret" in output
    assert "failed to write OpenBao secret" in output
