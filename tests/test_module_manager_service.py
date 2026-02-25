from types import SimpleNamespace

from fastapi import HTTPException

from app.services import module_manager


def test_load_module_states_defaults_true_when_settings_missing(monkeypatch):
    monkeypatch.setattr(module_manager.SettingsCache, "get", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(module_manager.SettingsCache, "set", staticmethod(lambda *_args, **_kwargs: True))

    def _missing(_db, _key):
        raise HTTPException(status_code=404, detail="missing")

    monkeypatch.setattr(module_manager.domain_settings_service.modules_settings, "get_by_key", _missing)
    states = module_manager.load_module_states(db=object(), force_refresh=True)
    assert states["billing"] is True
    assert states["network"] is True
    assert states["reports"] is True


def test_load_module_states_prefers_cached_value(monkeypatch):
    cached = {"billing": False, "catalog": True}
    monkeypatch.setattr(module_manager.SettingsCache, "get", staticmethod(lambda *_args, **_kwargs: cached))
    states = module_manager.load_module_states(db=object(), force_refresh=False)
    assert states["billing"] is False
    assert states["catalog"] is True


def test_update_module_flags_upserts_and_invalidates(monkeypatch):
    upserts: list[tuple[str, bool]] = []
    invalidations: list[tuple[str, str]] = []

    def _fake_upsert(_db, key, enabled):
        upserts.append((key, enabled))

    def _fake_invalidate(domain, key):
        invalidations.append((domain, key))
        return True

    monkeypatch.setattr(module_manager, "_upsert_boolean_setting", _fake_upsert)
    monkeypatch.setattr(module_manager.SettingsCache, "invalidate", staticmethod(_fake_invalidate))

    module_manager.update_module_flags(
        db=object(),
        payload={"billing": False, "catalog": True, "unknown": False},
    )

    assert ("module_billing_enabled", False) in upserts
    assert ("module_catalog_enabled", True) in upserts
    assert ("modules", "states") in invalidations
    assert ("modules", "feature_states") in invalidations


def test_load_feature_states_reads_value_json(monkeypatch):
    monkeypatch.setattr(module_manager.SettingsCache, "get", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(module_manager.SettingsCache, "set", staticmethod(lambda *_args, **_kwargs: True))

    def _get_by_key(_db, key):
        if key == "module_billing_invoices_enabled":
            return SimpleNamespace(value_json=False, value_text="true")
        raise HTTPException(status_code=404, detail="missing")

    monkeypatch.setattr(module_manager.domain_settings_service.modules_settings, "get_by_key", _get_by_key)
    states = module_manager.load_feature_states(db=object(), force_refresh=True)
    assert states["invoices"] is False
    assert states["payments"] is True

