from pathlib import Path


def _template(path: str) -> str:
    return Path(path).read_text()


def test_manual_payment_and_credit_forms_confirm_and_bound_amounts():
    payment_form = _template("templates/admin/billing/payment_form.html")
    payment_amount = _template("templates/admin/billing/_payment_amount_field.html")
    credit_form = _template("templates/admin/billing/credit_form.html")

    assert "Record this payment and allocate it" in payment_form
    assert "button[type=submit]').disabled = true" in payment_form
    assert 'min="0.01"' in payment_amount
    assert "Issue this credit to the selected billing account?" in credit_form
    assert "button[type=submit]').disabled = true" in credit_form
    assert 'min="0.01"' in credit_form


def test_dunning_and_routing_actions_confirm_before_state_changes():
    dunning = _template("templates/admin/billing/dunning.html")
    arrangement = _template("templates/admin/billing/payment_arrangement_detail.html")
    channels = _template("templates/admin/billing/payment_channels.html")
    collection_accounts = _template("templates/admin/billing/collection_accounts.html")

    assert "Pause all selected dunning cases?" in dunning
    assert "Resume all selected dunning cases?" in dunning
    assert "Pause this dunning case?" in dunning
    assert "Resume this dunning case?" in dunning
    assert "Close this dunning case?" in dunning
    assert "Approve this payment arrangement?" in arrangement
    assert "Deactivate this payment channel?" in channels
    assert "Deactivate this collection account?" in collection_accounts
