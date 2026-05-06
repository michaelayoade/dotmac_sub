from app.services.network.equipment_identity import normalize_ont_equipment_id


def test_normalize_ont_equipment_id_keeps_ont_models():
    assert normalize_ont_equipment_id(" EG8145V5 ") == "EG8145V5"
    assert normalize_ont_equipment_id("HG8145V5") == "HG8145V5"


def test_normalize_ont_equipment_id_rejects_olt_chassis_models():
    assert normalize_ont_equipment_id("MA5600") is None
    assert normalize_ont_equipment_id("MA5608T") is None
    assert normalize_ont_equipment_id("MA5800-X2") is None
    assert normalize_ont_equipment_id("MA5600V800R013C10") is None
